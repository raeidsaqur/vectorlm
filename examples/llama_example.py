from __future__ import annotations

import argparse
import math
import os
import sys
from argparse import Namespace

import torch
import torch.distributed as dist
from torch.optim import AdamW
from tqdm import tqdm
from transformers import set_seed
from vllm import SamplingParams

from vectorlm.dataset import Dataset
from vectorlm.sampling import LoRASamplingEngine
from vectorlm.sampling.utils import handle_sample
from vectorlm.trainer import Trainer
from vectorlm.utils.data_utils import Config
from vectorlm.utils.misc_utils import cleanup, setup, wandb_setup
from vectorlm.utils.model_utils import (
    get_lora_model_from_base_model,
    get_submodule_by_pattern,
    load_model_and_tokenizer,
    shard_model,
)
from vectorlm.utils.optimizer_utils import get_custom_scheduler
from vectorlm.utils.save_utils import (
    checkpoint_exists,
    get_latest_checkpoint_dir,
    save_consolidated_model,
    save_peft_adapter,
)


def parse_args() -> Namespace:
    """Parse command-line arguments.

    Returns
    -------
        The parsed arguments.

    """
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--yaml_path",
        default="configs/config.yaml",
        required=False,
    )
    return parser.parse_args()


def main(config: Config) -> None:
    """Define the main calling function."""
    training_args = config.train_parameters

    # set a seed
    set_seed(training_args.seed)

    # set CUDA related dependencies
    local_rank = int(os.environ["LOCAL_RANK"])
    rank = int(os.environ["RANK"])
    world_size = int(os.environ["WORLD_SIZE"])

    print(f"Rank: {rank}, World size: {world_size}")
    if dist.is_initialized():
        torch.cuda.set_device(local_rank)
        torch.cuda.empty_cache()

    # setup wandb
    if rank == 0:
        wandb_setup(config, **config.wandb_config)
    dist.barrier()

    # load model and tokenizer
    model, tokenizer = load_model_and_tokenizer(
        config.model,
        training_args.use_mp,
        training_args.use_flash_attention,
        training_args.max_seq_len,
        local_rank,
        training_args.low_cpu_mem_usage,
    )

    lora_peft_config = getattr(
        config.train_parameters,
        "lora_peft_config",
        None,
    )
    is_peft_adapter_restored = False
    if lora_peft_config is not None:
        peft_adapter_path = None
        # Restore peft adapter from filesystem if available.
        if checkpoint_exists(training_args.output_dir):
            peft_adapter_path = os.path.join(
                training_args.output_dir,
                "checkpoints",
                get_latest_checkpoint_dir(
                    os.path.join(training_args.output_dir, "checkpoints"),
                ),
            )
            is_peft_adapter_restored = True

        model = get_lora_model_from_base_model(
            model,
            lora_peft_config,
            peft_adapter_path,
        )

    decoder_layer_module = get_submodule_by_pattern(model, r"DecoderLayer$")
    assert decoder_layer_module is not None, f"No DecoderLayer found in {model}"
    model = shard_model(
        model,
        decoder_layer_module,
        training_args.use_mp,
        training_args.use_activation_checkpointing,
        training_args.sharding_strategy,
        local_rank,
        training_args.low_cpu_mem_usage,
        is_lora_enabled=(lora_peft_config is not None),
    )

    # load dataset
    dataset = Dataset(
        config=config.dataset,
        tokenizer=tokenizer,
    )

    # instantiate trainer
    trainer = Trainer(
        config=training_args,
        enable_wandb_logging=config.enable_wandb_logging,
        original_dataset_length=dataset.original_length,
    )

    # load optimizer
    optimizer = AdamW(
        model.parameters(),
        **training_args.optimizer,
    )

    # load lr scheduler
    lr_scheduler = get_custom_scheduler(
        training_args.lr_scheduler_type,
        optimizer,
        math.ceil(
            trainer.num_update_steps_per_epoch * training_args.warmup_ratio,
        ),
        trainer.max_steps,
    )

    trainer.prepare_trainer(
        model,
        tokenizer,
        dataset,
        optimizer,
        lr_scheduler,
        is_peft_adapter_restored,
    )

    # Checkpoint check. Always call before training.
    # If no checkpoint, it returns 0.
    checkpointed_epoch = trainer.find_checkpoint(training_args.output_dir)

    if training_args.sampler is not None:
        sampling_engine = LoRASamplingEngine(
            trainer,
            SamplingParams(seed=0),
            gpu_memory_utilization=0.3,
            base_model_name=config.model,
        )

    for epoch in range(checkpointed_epoch, training_args.epochs):
        train_dl_iterator = iter(dataset.train_dataloader)
        for index in tqdm(
            range(len(dataset.train_dataloader)),
            disable=rank != 0,
            file=sys.__stdout__,
        ):
            batch = next(train_dl_iterator)
            trainer.step(batch, epoch)

            if (
                (training_args.sampler is not None)
                and (index % training_args.sampler.sample_frequency == 0)
                and (dist.get_rank() == 0)
            ):
                sampling_engine.update(trainer)
                handle_sample(
                    sampling_engine,
                    training_args.sampler.prompts,
                    training_args.sampler.output_jsonl_path,
                    extra_data={"tr_step": trainer.tr_step},
                )

        if epoch == training_args.epochs - 1:
            hf_save_dir = os.path.join(training_args.output_dir, "final-model")
        else:
            hf_save_dir = os.path.join(
                training_args.output_dir,
                "checkpoints",
                f"epoch_{epoch}",
                "end-epoch-model",
            )
        # Save base (consolidated) model only when not running peft.
        if lora_peft_config is None:
            save_consolidated_model(trainer.model, hf_save_dir, rank)
        else:
            save_peft_adapter(trainer.model, hf_save_dir)

        dataset.reset_dataloaders()


if __name__ == "__main__":
    args = parse_args()
    config = Config(yaml_path=args.yaml_path)
    setup(config.train_parameters.output_dir)
    main(config)
    cleanup()
