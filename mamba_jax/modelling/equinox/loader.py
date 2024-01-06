import json
from typing import Dict, Optional

import equinox as eqx
import jax
import jax.numpy as jnp
import torch  # TODO: can remove dependency once mamba converted to safetensors
from huggingface_hub import hf_hub_download
from transformers import AutoTokenizer

from .model import MambaLLM


def get_pt_checkpoint(repo_id: str, config_path: str = "config.json", checkpoint_path: str = "pytorch_model.bin"):
    config_path = hf_hub_download(repo_id=repo_id, filename=config_path)
    checkpoint_path = hf_hub_download(repo_id=repo_id, filename=checkpoint_path)

    with open(config_path, mode="r") as f:
        config = json.load(f)

    sd = torch.load(checkpoint_path, weights_only=True, map_location="cpu")

    return sd, config


def pt_to_raw_pytree(sd, dtype: Optional[jnp.dtype] = jnp.float32):
    def _key_rename(k: str):
        # TODO: consider just renaming this in modelling code 🤷
        k = k.replace("backbone", "model")
        return k

    if dtype is None:
        return {_key_rename(k): jnp.asarray(v) for k, v in sd.items()}
    return {_key_rename(k): jnp.asarray(v, dtype=dtype) for k, v in sd.items()}


# TODO: add test for this, comparing all weights as a sanity check
def init_mamba_from_raw_pytree(tree, config):
    # TODO: use other options from config
    N = config["d_model"]
    num_layers = config["n_layer"]
    vocab_size = config["vocab_size"]
    model = MambaLLM(N, num_layers, vocab_size, key=jax.random.PRNGKey(0), dtype=config["dtype"])

    def where_fn(model):
        where = []
        for k in tree.keys():
            path = k.split(".")

            node = model
            for p in path:
                if p.isnumeric():
                    node = node[int(p)]
                else:
                    node = getattr(node, p)

            where.append(node)

        return where

    def generate_replace(tree):
        replace = []
        for k, v in tree.items():
            # paranoid about ordering..
            if "conv1d.bias" in k:
                replace.append(jnp.expand_dims(v, axis=-1))
                continue
            replace.append(v)

        return replace

    replace = generate_replace(tree)
    model = eqx.tree_at(where_fn, model, replace=replace)

    return model


def load_pretrained(model, dtype: jnp.dtype = jnp.float32):
    sd, config = get_pt_checkpoint(model)
    config["dtype"] = dtype
    tree = pt_to_raw_pytree(sd, dtype=config["dtype"])
    model = init_mamba_from_raw_pytree(tree, config)
    tokenizer = AutoTokenizer.from_pretrained("EleutherAI/gpt-neox-20b")
    return model, tokenizer
