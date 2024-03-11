"""
Create SLURM jobs running the LoRA benchmark. 
"""

import argparse
import itertools
import subprocess
import time
from typing import List

from tqdm.auto import tqdm

parser = argparse.ArgumentParser()
parser.add_argument("--qos", required=False)
cli_args = parser.parse_args()
qos_selected = cli_args.qos

model_list = [
    "/model-weights/" + model_name
    for model_name in [
        "opt-350m",
        "Llama-2-7b-hf",
        "Llama-2-13b-hf",
        "Mistral-7B-v0.1",
    ]
]

slurm_flags_options = {
    "nodes": [1],
    "mem": [0],
    "ntasks-per-node": [1],
    "cpus-per-gpu": [6],
    "gres": ["gpu:{}".format(n + 1) for n in range(8)],
    "partition": ["t4v2", "a40", "a100"],
}

slurm_flags_extra = {"time": "00:30:00", "qos": qos_selected}

slurm_pos_args_options = [["examples/launch_lora_benchmark.sh"], model_list]
timestamp = int(time.time())

args_list: List[List[str]] = []
for index, (flag_values, pos_args_option) in enumerate(
    itertools.product(
        itertools.product(*(slurm_flags_options.values())),
        itertools.product(*slurm_pos_args_options),
    )
):
    args: List[str] = ["sbatch"]

    extra_flags = {
        **slurm_flags_extra,
        "output": "data/output/{}.{}.out".format(timestamp, index),
        "error": "data/output/{}.{}.out".format(timestamp, index),
        "job-name": "bench-{}-{}".format(timestamp, index),
    }

    keys = list(slurm_flags_options.keys()) + list(extra_flags.keys())
    values = list(flag_values) + list(extra_flags.values())
    for key, value in zip(keys, values):
        if value is not None:
            arg = ("--{}".format(key), str(value))
            args.extend(arg)

    args.extend(pos_args_option)
    args_list.append(args)
    print(" ".join(args))

input("\nPress ENTER to launch {} job(s)".format(len(args_list)))

for args in tqdm(args_list):
    subprocess.run(args)
