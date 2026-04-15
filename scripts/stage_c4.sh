#!/usr/bin/env bash
set -euo pipefail

KUBE_CONTEXT="${KUBE_CONTEXT:-nebius-nebius-poc-slurm}"
KUBE_NAMESPACE="${KUBE_NAMESPACE:-soperator}"
LOGIN_POD="${LOGIN_POD:-login-0}"
JAIL_DATASET_DIR="${JAIL_DATASET_DIR:-/home/nebius/datasets/c4}"
TOKEN_TARGET="${TOKEN_TARGET:-100000000}"

kubectl --context "${KUBE_CONTEXT}" -n "${KUBE_NAMESPACE}" exec "${LOGIN_POD}" -- \
  chroot /mnt/jail /bin/bash -lc "
set -euo pipefail
mkdir -p '${JAIL_DATASET_DIR}'
/home/nebius/venv/bin/python - <<'PY'
import json
from pathlib import Path

from datasets import load_dataset
from tokenizers import Tokenizer

local_dir = '${JAIL_DATASET_DIR}'
token_target = int('${TOKEN_TARGET}')
tokenizer_path = Path('/home/nebius/.cache/huggingface/assets/Llama-3.1-8B/tokenizer.json')
manifest_path = Path(local_dir) / 'bootstrap_manifest.json'

tokenizer = Tokenizer.from_file(str(tokenizer_path))
dataset = load_dataset('allenai/c4', name='en', split='train', streaming=True)

docs = 0
tokens = 0
first = None

for sample in dataset:
    text = sample['text']
    n_tokens = len(tokenizer.encode(text).ids)
    docs += 1
    tokens += n_tokens
    if first is None:
        first = {
            'keys': sorted(sample.keys()),
            'timestamp': sample.get('timestamp'),
            'url': sample.get('url'),
            'text_chars': len(text),
            'text_tokens': n_tokens,
        }
    if tokens >= token_target:
        break

manifest = {
    'dataset': 'allenai/c4',
    'subset': 'en',
    'mode': 'streaming',
    'token_target': token_target,
    'tokens_streamed': tokens,
    'documents_streamed': docs,
    'tokenizer_path': str(tokenizer_path),
    'sample': first,
}
manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
print(manifest)
PY
"
