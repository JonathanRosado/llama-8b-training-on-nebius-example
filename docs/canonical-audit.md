# Canonical Audit

Purpose: prove the live Path B PoC stays inside Nebius-preferred building blocks where those building blocks are usable, and record the exact places where the implementation intentionally diverges because of confirmed constraints.

Scope: four axes only. Evidence is limited to committed repo artifacts and live Path B configuration under `soperator/installations/poc/`.

## Summary

| Axis | Canonical choice | Repo evidence | Audit result | Interview defense |
|---|---|---|---|---|
| Marketplace / Solutions Library alignment | Use Nebius-provided marketplace releases and Solutions Library patterns where available | `modules/gpu-operator/main.tf:1-8`, `modules/network-operator/main.tf:1-8`, `modules/o11y/prometheus.tf:11-19`, `terraform/path-a/helm.tf:68-82` | **Aligned with bounded deviations** | The PoC uses Nebius marketplace releases for platform services and self-deployed Soperator from the Solutions Library tree. The main deviation is Path B being self-deployed Soperator rather than Managed Soperator, which is explicitly consistent with the one-week execution plan in `Runbook-final.md:177-183`. |
| GPU interconnect strategy | Ethernet for quota-constrained fallback; InfiniBand for production-shaped training | `training/sbatch/fsdp_demo.sbatch` (Path A Ethernet), `soperator/installations/poc/fsdp_demo.sbatch:13-18`, `docs/training-efficiency.md:1-6`, `Runbook-final.md:151-153` | **Aligned** | Path A proves the fallback transport stack. Path B is the production-shaped answer: Soperator + InfiniBand + Slurm, using `NCCL_IB_HCA=mlx5` and `NCCL_IB_GID_INDEX=3` on the live cluster. |
| Training framework | TorchTitan with parameterized FSDP2 / HSDP / TP configs that preserve the 500-GPU shape | `training/titan/configs/fsdp_full.toml`, `hsdp.toml`, `tp_dp.toml`, `docs/training-efficiency.md:11-15`, `docs/scaling-to-100b.md:36-60` | **Aligned** | The PoC validated three TorchTitan strategies on LLaMA-3.1-8B and kept the scale knobs in config, not in ad hoc launcher logic. That is the staff-level part: same code shape, different parallelism degrees. |
| Observability stack | TensorBoard for native TorchTitan scalars, Nebius metrics for infra, MLflow for experiment registry/UI | `docs/training-efficiency.md:36-40`, `Runbook-final.md:159-163`, `modules/o11y/`, `soperator/test/common/mlflow.sh.sample:3-12` | **Partially aligned; gap recorded** | Infra observability is Nebius-native and live. TensorBoard is live. MLflow is the defensible OSS tracker choice, but it is not yet deployed on Path B, so this axis is intentionally marked incomplete rather than overstated. |

## Axis 1: Marketplace / Solutions Library Alignment

| Question | Evidence | Assessment |
|---|---|---|
| Are platform add-ons installed through Nebius-supported release objects rather than hand-rolled Helm where a marketplace release exists? | `nebius_applications_v1alpha1_k8s_release` is used for GPU operator, network operator, Prometheus, Loki, Grafana, Ray, and device plugin in `modules/` and `terraform/path-a/helm.tf`. | **Yes.** The repo already established the preferred pattern: consume Nebius marketplace products through Terraform resources, not bespoke shell installs. |
| Is Slurm-on-Kubernetes aligned to Solutions Library structure? | Path B lives in `soperator/installations/poc/` and imports from the vendored `soperator/modules/` tree. `Runbook-final.md:177-183` marks self-deployed Soperator as the executed must-have path. | **Yes.** This is canonical enough for a PoC because it uses the Solutions Library implementation directly. |
| What is the deliberate deviation? | `docs/scaling-to-100b.md:138-142` states Path B is self-deployed Soperator, not Managed Soperator. | **Accepted deviation.** The interview defense is tenant/time reality, not preference drift. The repo already documents Managed Soperator as the preferred production control-plane option when directly usable. |

## Axis 2: GPU Interconnect Strategy

| Decision | Evidence | Assessment |
|---|---|---|
| Path A remains Ethernet-only | Path A validator and launcher documentation keep `NCCL_IB_DISABLE=1`; the runbook frames Path A as the constrained fallback path. | **Correct for Path A.** It demonstrates portability under quota constraints, not the target operating model. |
| Path B uses InfiniBand as primary transport | `soperator/installations/poc/fsdp_demo.sbatch:42-48` unsets Ethernet-era knobs and exports `NCCL_IB_HCA=mlx5`, `NCCL_IB_GID_INDEX=3`. `docs/training-efficiency.md:3` records NDR400 IB. | **Canonical.** This is the right transport posture for Nebius H200 training clusters. |
| Is the performance evidence real, not architectural intent? | `docs/training-efficiency.md:11-15` records `fsdp_full=7,785 tok/s/GPU`, `hsdp=7,752`, `tp_dp=2,660`. `CHANGELOG.md:320-326` records the Path B validator and jail-mode decisions that made the runs possible. | **Yes.** The stack is not just described; it was benchmarked. |

## Axis 3: Training Framework

| Question | Evidence | Assessment |
|---|---|---|
| Is the PoC training framework the same family intended for the 500-GPU story? | `docs/scaling-to-100b.md:3-4` says the TorchTitan code in `training/titan/` is intentionally shaped for the 512-GPU target. | **Yes.** This is the strongest architectural decision in the repo. |
| Were multiple production-relevant parallelism strategies validated? | `docs/training-efficiency.md:11-15` covers FSDP2 full-shard, HSDP, and TP+DP. | **Yes.** The PoC tested the knobs that matter at scale, not just one happy path. |
| Is the dataset path still a weak point? | `docs/training-efficiency.md:5` and `docs/scaling-to-100b.md:93-103` explicitly say C4 is still HF-streamed in the PoC and must move to staged Object Storage plus local cache for 500+ GPU. | **Known gap.** This is correctly called out in-repo and should stay in the audit as unfinished, not silently normalized. |

## Axis 4: Observability Stack

| Layer | Evidence | Assessment |
|---|---|---|
| Job-level training telemetry | `docs/training-efficiency.md:38` confirms TorchTitan 0.2.2 TensorBoard emits `loss`, `grad_norm`, `lr`, `memory`, but not `tps`/`mfu`. | **Partially sufficient.** Good enough for PoC loss/memory traces, not enough for a polished experiment-tracking surface. |
| Cluster / infra telemetry | `modules/o11y/` contains Prometheus, Grafana, Loki, and the Nebius observability agent. `Runbook-final.md:161-163` requires Nebius infra metrics and Slurm observability. | **Aligned.** Infra monitoring follows Nebius-native components. |
| Experiment tracker choice | `soperator/test/common/mlflow.sh.sample:3-12` shows MLflow is already recognized in the repo as the expected external tracking endpoint shape. | **Directionally aligned, not deployed.** The repo supports the choice, but Path B still lacks the release wiring and training wrapper. |

## Audit Verdict

The live PoC is strongest on three axes: platform alignment, interconnect choice, and training-framework shape. The only material canonical gap is the observability/data layer above native TensorBoard: MLflow is selected but not deployed, and the C4 production path is documented but not yet implemented. That is defensible because the repo already distinguishes executed PoC evidence from target-state scale guidance instead of pretending those are the same thing.

## Follow-up

Flux Git authentication on Path B is bootstrapped with a read-only GitHub deploy key stored in the `flux-gitops-main-auth` Secret in `flux-system`. That is acceptable as an initial bootstrap for a PoC, but the staff-level follow-up is to migrate this Secret to SOPS-encrypted GitOps state before calling the design production-ready. The migration story is: keep the deploy key read-only, move the Secret manifest under Git with SOPS, and remove the out-of-band kubectl bootstrap once Flux can decrypt and reconcile it.
