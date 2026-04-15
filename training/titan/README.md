# TorchTitan Path B Harness

This directory vendors TorchTitan for the Nebius Path B PoC and pins it to a
specific upstream main-branch commit for reproducibility.

- Upstream repository: `https://github.com/pytorch/torchtitan`
- Pinned upstream SHA: `74485ea3381d7c1739b004daace3d9ecaead67e9`
- Upstream config basis: `torchtitan.models.llama3.config_registry.llama3_8b()`

Important context:

- Current TorchTitan `HEAD` no longer ships `torchtitan/models/llama3/train_configs/llama3_8b.toml`.
- The pinned snapshot uses the Python config registry plus CLI overrides.
- The TOML files in `training/titan/configs/` are therefore launcher inputs that
  map onto the current upstream config registry fields.

Operational notes:

- These jobs are intended to run inside the Soperator jail on Path B.
- The PoC uses the jail-resident torch stack at `/home/nebius/venv`.
- That pyxis bypass is acceptable for this PoC only; it is not the production
  pattern to recommend at 500+ GPU scale.
- Llama 3.1 8B runs are configured to load HF safetensors from
  `/home/nebius/.cache/huggingface/assets/Llama-3.1-8B`.

Files:

- `configs/fsdp_full.toml`: 16-way full sharding baseline on 2x8 H200.
- `configs/hsdp.toml`: 2 replica groups x 8-way shard groups.
- `configs/tp_dp.toml`: TP=2 plus replicated DP groups.
- `launch.sh`: renders and submits an `sbatch` job for one config.

Usage inside the jail:

```bash
cd /home/nebius/training/titan
./launch.sh fsdp_full
./launch.sh hsdp
./launch.sh tp_dp
```

By default, each run writes under `/home/nebius/training-runs/<config>/<jobid>/`.
The launcher also saves the resolved TorchTitan config JSON for each run so the
exact effective parameters are auditable after submission.
