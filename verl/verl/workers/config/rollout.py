# Copyright 2025 Bytedance Ltd. and/or its affiliates
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import warnings
from dataclasses import dataclass, field
from typing import Optional

from omegaconf import MISSING

from verl.base_config import BaseConfig
from verl.utils.profiler import ProfilerConfig
from verl.workers.config.model import MtpConfig

__all__ = [
    "SamplingConfig",
    "DiffusionSamplingConfig",
    "MultiTurnConfig",
    "CustomAsyncServerConfig",
    "AgentLoopConfig",
    "TraceConfig",
    "ServerConfig",
    "PrometheusConfig",
    "RolloutConfig",
    "DiffusionRolloutConfig",
    "CheckpointEngineConfig",
]


@dataclass
class SamplingConfig(BaseConfig):
    temperature: float = 1.0
    top_k: int = -1
    top_p: float = 1.0
    do_sample: bool = True
    n: int = 1


@dataclass
class DiffusionSamplingConfig(BaseConfig):
    do_sample: bool = True
    n: int = 1
    noise_level: float = 0.0
    num_inference_steps: int = 40
    seed: int = 42


@dataclass
class MultiTurnConfig(BaseConfig):
    _mutable_fields = {
        "max_assistant_turns",
        "max_user_turns",
        "context_reset_enabled",
        "context_reset_threshold",
    }

    enable: bool = False
    max_assistant_turns: Optional[int] = None
    tool_config_path: Optional[str] = None
    max_user_turns: Optional[int] = None
    max_parallel_calls: int = 1
    max_tool_response_length: int = 256
    tool_response_truncate_side: str = "middle"
    interaction_config_path: Optional[str] = None
    use_inference_chat_template: bool = False
    tokenization_sanity_check_mode: str = "strict"
    format: str = "hermes"
    num_repeat_rollouts: Optional[int] = None
    # Context management (ported from OpenResearcher researcher_v2)
    # When len(generation_prompt) >= threshold, drop intermediate tool-call history
    # from the vLLM prompt while keeping the full trajectory in prompt_ids for training.
    # context_reset_threshold=0 disables the mechanism at runtime even if enabled=True.
    context_reset_enabled: bool = False
    context_reset_threshold: int = 0
    context_reset_message: str = (
        "[Context was reset to manage length. The original question is above. "
        "Continue your research — you may search again or submit your answer.]"
    )
    # Optional summarizer that runs before the reset fires: when enabled, calls
    # Claude Haiku via AWS Bedrock to condense the discarded history into bullet
    # points of key findings, then uses that summary as the reset note. Falls
    # back to the static context_reset_message above if the call fails.
    # Credentials: context_reset_summarizer_aws_{key,secret} OR env
    # CONTEXT_SUMMARIZER_AWS_ACCESS_KEY_ID / _SECRET_ACCESS_KEY OR
    # CONTEXT_SUMMARIZER_BEDROCK_KEY (base64 "key_id:secret" combo).
    context_reset_summarizer_enabled: bool = False
    context_reset_summarizer_model: str = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
    context_reset_summarizer_region: str = "us-east-1"
    context_reset_summarizer_max_tokens: int = 1024
    context_reset_summarizer_aws_key: Optional[str] = None
    context_reset_summarizer_aws_secret: Optional[str] = None
    # Naive sliding-window reset (v5m): when the generation prompt crosses
    # `context_reset_threshold`, rebuild it as
    #   [system, original_user, *messages[-context_reset_keep_last_rounds*2:]]
    # dropping the middle rounds. Cheaper than the summarizer and keeps the
    # most recent tool/assistant turns verbatim. USED ONLY WHEN
    # context_reset_mode == "sliding_window".
    context_reset_keep_last_rounds: int = 4
    # Max number of context resets per rollout. When the count is hit, the
    # next overflow falls through to `_maybe_force_answer` (last resort).
    context_reset_max_count: int = 10
    # Which reset body to build on overflow:
    #   "summarizer" — Claude-Haiku summary or static fallback note (v5h default,
    #                  best for keeping rollouts submitting).
    #   "sliding_window" — keep system + user + last N rounds verbatim (v5m).
    context_reset_mode: str = "summarizer"
    # Soft token-count cap for force-answer. When response_mask length crosses
    # this threshold, the next turn-limit event injects a finish-tool prefix
    # so the model spends its remaining (response_length − threshold) tokens
    # emitting the answer rather than continuing to explore. 0 disables
    # (force-answer only fires at response_length exhaustion, the old
    # behavior).
    force_answer_token_threshold: int = 0
    # Force-answer variant for SFT-ed models that submit via ehr.finish tool
    # call rather than a free-text <answer>…</answer> tag. When enabled, the
    # last-resort _maybe_force_answer path injects a Qwen3-XML tool-call
    # prefix (…<tool_call><function=ehr.finish>…<parameter=answer>) instead
    # of the <answer>… text prefix. Leave disabled for base / non-SFT models.
    force_finish_tool_enabled: bool = False
    force_finish_tool_name: str = "ehr.finish"
    # SFT-ed Qwen3.5 model was trained with `response` as the finish-tool
    # argument name (not `answer`). Default matches the SFT emission format.
    force_finish_tool_param: str = "response"


@dataclass
class CustomAsyncServerConfig(BaseConfig):
    path: Optional[str] = None
    name: Optional[str] = None


@dataclass
class AgentLoopConfig(BaseConfig):
    num_workers: int = 8
    default_agent_loop: str = "single_turn_agent"
    agent_loop_config_path: Optional[str] = None
    custom_async_server: CustomAsyncServerConfig = field(default_factory=CustomAsyncServerConfig)
    # Fully qualified class name for custom AgentLoopManager (e.g., "mypackage.module.MyManager").
    # Security: This class will be dynamically imported via importlib. Only use trusted class paths.
    agent_loop_manager_class: Optional[str] = None


@dataclass
class TraceConfig(BaseConfig):
    project_name: Optional[str] = None
    experiment_name: Optional[str] = None
    backend: Optional[str] = None
    token2text: bool = False
    max_samples_per_step_per_worker: Optional[int] = None

    def __post_init__(self):
        if self.max_samples_per_step_per_worker is not None and self.max_samples_per_step_per_worker < 0:
            raise ValueError("`max_samples_per_step_per_worker` must be a non-negative integer or null.")


@dataclass
class ServerConfig(BaseConfig):
    """
    Configuration for SGLang server when running in server mode
    """

    timeout: float = 60.0
    max_attempts: int = 3
    retry_delay: float = 2.0
    max_connections: int = 1000
    max_start_wait_time: float = 300.0


@dataclass
class PrometheusConfig(BaseConfig):
    """
    Configuration for Prometheus server
    """

    # whether enable prometheus on server mode rollout
    enable: bool = False
    # Port number that Prometheus listens on, default is 9090
    port: int = 9090
    # Path to Prometheus configuration file
    file: str = "/tmp/ray/session_latest/metrics/prometheus/prometheus.yml"
    # Specify served_model_name to avoid displaying overly long model paths in Grafana
    served_model_name: Optional[str] = None


@dataclass
class CheckpointEngineConfig(BaseConfig):
    """
    Configuration for checkpoint engine to update weights from trainer to rollout
    """

    # Backend for checkpoint engine: naive, nccl, nixl, hccl
    backend: Optional[str] = "naive"
    # Bucket size in MB to transfer multiple weights at one time
    update_weights_bucket_megabytes: int = 2048
    # Additional keyword arguments for checkpoint engine
    engine_kwargs: dict = field(default_factory=dict)


@dataclass
class RolloutConfig(BaseConfig):
    _mutable_fields = {
        "max_model_len",
        "load_format",
        "engine_kwargs",
        "prompt_length",
        "response_length",
        "expert_parallel_size",
        "moe_tensor_parallel_size",
    }

    name: Optional[str] = MISSING
    mode: str = "async"
    nnodes: int = 0
    n_gpus_per_node: int = 8

    temperature: float = 1.0
    top_k: int = -1
    top_p: float = 1.0
    do_sample: bool = True
    n: int = 1
    repetition_penalty: float = 1.0

    # Early termination threshold for multi-turn rollout in sglang.
    # Abort remaining requests when (1 - over_sample_rate) * total_requests are completed.
    over_sample_rate: float = 0.0

    prompt_length: int = 512
    response_length: int = 512

    dtype: str = "bfloat16"
    gpu_memory_utilization: float = 0.5
    ignore_eos: bool = False
    enforce_eager: bool = True
    cudagraph_capture_sizes: Optional[list] = None
    free_cache_engine: bool = True
    data_parallel_size: int = 1
    expert_parallel_size: int = 1
    tensor_model_parallel_size: int = 2
    pipeline_model_parallel_size: int = 1
    moe_tensor_parallel_size: int = 1
    max_num_batched_tokens: int = 8192
    logprobs_mode: Optional[str] = "processed_logprobs"
    scheduling_policy: Optional[str] = "fcfs"

    # TODO: enable train_kwargs
    # train_sampling_config: SamplingConfig = field(default_factory=SamplingConfig)

    val_kwargs: SamplingConfig = field(default_factory=SamplingConfig)

    max_model_len: Optional[int] = None
    max_num_seqs: int = 1024

    # note that the logprob computation should belong to the actor
    log_prob_micro_batch_size: Optional[int] = None
    log_prob_micro_batch_size_per_gpu: Optional[int] = None
    log_prob_use_dynamic_bsz: bool = False
    log_prob_max_token_len_per_gpu: int = 16384

    disable_log_stats: bool = True

    multi_stage_wake_up: bool = False
    engine_kwargs: dict = field(default_factory=dict)

    calculate_log_probs: bool = False

    agent: AgentLoopConfig = field(default_factory=AgentLoopConfig)

    trace: TraceConfig = field(default_factory=TraceConfig)

    multi_turn: MultiTurnConfig = field(default_factory=MultiTurnConfig)

    # Server configuration for sglang server mode
    server: ServerConfig = field(default_factory=ServerConfig)

    # Use Prometheus to collect and monitor rollout statistics
    prometheus: PrometheusConfig = field(default_factory=PrometheusConfig)

    # Extension point for custom configurations
    custom: Optional[dict] = None

    # Checkpoint Engine config for update weights from trainer to rollout
    checkpoint_engine: CheckpointEngineConfig = field(default_factory=CheckpointEngineConfig)

    skip_rollout: bool = False

    skip_dump_dir: str = "/tmp/rollout_dump"

    profiler: Optional[ProfilerConfig] = None

    enable_chunked_prefill: bool = True

    enable_prefix_caching: bool = True

    load_format: str = "dummy"

    layered_summon: bool = False

    layer_name_map: dict = field(default_factory=dict)

    sglang_engine_mode: str = "local"

    limit_images: Optional[int] = None

    skip_tokenizer_init: bool = False

    quantization: Optional[str] = None

    quantization_config_file: Optional[str] = None

    enable_rollout_routing_replay: bool = False

    enable_sleep_mode: bool = True

    mtp: MtpConfig = field(default_factory=MtpConfig)

    qat: Optional[dict] = None

    def __post_init__(self):
        """Validate the rollout config"""
        # Deprecation warning for mode field - only async mode is supported
        if self.mode == "sync":
            raise ValueError(
                "Rollout mode 'sync' has been removed. Please set "
                "`actor_rollout_ref.rollout.mode=async` or remove the mode setting entirely."
            )
        if self.mode != "async":
            warnings.warn(
                f"Unknown rollout mode '{self.mode}'. Only 'async' mode is supported. "
                "The 'mode' field is deprecated and will be removed in a future version.",
                DeprecationWarning,
                stacklevel=2,
            )

        if self.name != "trtllm" and self.expert_parallel_size > 1:
            assert self.expert_parallel_size == (self.tensor_model_parallel_size * self.data_parallel_size), (
                "expert_parallel_size must be equal to tensor_model_parallel_size * data_parallel_size"
            )

        if self.moe_tensor_parallel_size is not None and self.moe_tensor_parallel_size > 1:
            assert self.name == "trtllm", "moe_tensor_parallel_size is only supported for trtllm"

        if self.name == "trtllm":
            # If either expert_parallel_size or moe_tensor_parallel_size is at default 1,
            # convert to None so TensorRT-LLM treats it as unspecified.
            # When both unspecified: moe_ep_size=1, moe_tp_size=moe_world_size (no EP, all TP).
            # When only one set: the other is auto-derived from tensor_model_parallel_size.
            if self.expert_parallel_size is not None and self.expert_parallel_size == 1:
                self.expert_parallel_size = None
            if self.moe_tensor_parallel_size is not None and self.moe_tensor_parallel_size == 1:
                self.moe_tensor_parallel_size = None
            if self.expert_parallel_size is not None and self.moe_tensor_parallel_size is not None:
                assert self.moe_tensor_parallel_size * self.expert_parallel_size == self.tensor_model_parallel_size, (
                    "moe_tensor_parallel_size * expert_parallel_size must equal tensor_model_parallel_size "
                    f"(got {self.moe_tensor_parallel_size} * {self.expert_parallel_size} = "
                    f"{self.moe_tensor_parallel_size * self.expert_parallel_size}, "
                    f"tensor_model_parallel_size={self.tensor_model_parallel_size})"
                )

        if self.pipeline_model_parallel_size > 1:
            if self.name == "vllm" or self.name == "sglang" or self.name == "trtllm":
                raise NotImplementedError(
                    f"Current rollout {self.name=} not implemented pipeline_model_parallel_size > 1 yet."
                )


@dataclass
class DiffusionRolloutConfig(RolloutConfig):
    _mutable_fields = {"max_model_len", "load_format"}

    val_kwargs: DiffusionSamplingConfig = field(default_factory=DiffusionSamplingConfig)

    # diffusion use
    height: int = 512

    width: int = 512

    num_inference_steps: int = 10

    guidance_scale: float = 4.5

    def __post_init__(self):
        """Validate diffusion rollout config"""
        super().__post_init__()

        if self.pipeline_model_parallel_size > 1 and self.name == "vllm_omni":
            raise NotImplementedError(
                f"Current rollout {self.name=} not implemented pipeline_model_parallel_size > 1 yet."
            )
