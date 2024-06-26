# Copyright (c) 2023, NVIDIA CORPORATION. All rights reserved.

"""Pretrain utilities."""

from datetime import datetime
import math
import sys
import time
import os
import re

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__),
                                             os.path.pardir)))

from megatron.model.yuan_hf_model import YuanForCausalLM
from megatron.core.enums import ModelType
# The earliest we can measure the start time.
_TRAIN_START_TIME = time.time()
import torch
from torch.nn.parallel.distributed import DistributedDataParallel as torchDDP
from megatron.core.enums import ModelType
from megatron import get_args
from megatron import get_signal_handler
from megatron import get_timers
from megatron import get_tensorboard_writer
from megatron import get_current_global_batch_size
from megatron import get_num_microbatches
from megatron import is_last_rank
from megatron import update_num_microbatches
from megatron.core import mpu, tensor_parallel
from megatron.core.utils import get_model_config
from megatron import print_rank_0
from megatron import print_rank_last
from megatron.checkpointing import save_checkpoint,_load_base_checkpoint,fix_query_key_value_ordering
from megatron.model import Float16Module
from megatron.model import GPTModel
from megatron.optimizer import get_megatron_optimizer
from megatron.initialize import initialize_megatron
from megatron.initialize import write_args_to_tensorboard
from megatron.initialize import set_jit_fusion_options
from megatron.optimizer_param_scheduler import OptimizerParamScheduler
from megatron.model import DistributedDataParallel as LocalDDP
from megatron.utils import check_adlr_autoresume_termination
from megatron.utils import unwrap_model
from megatron.data.data_samplers import build_pretraining_data_loader
from megatron.utils import calc_params_l2_norm
from megatron.core.pipeline_parallel import get_forward_backward_func
from megatron.utils import report_memory
from megatron.model.vision.knn_monitor import compute_feature_bank
from megatron.arguments import core_transformer_config_from_args
import copy
import transformers

from megatron.model import YuanModel



_CHECKPOINT_VERSION = None



def set_checkpoint_version(value):
    global _CHECKPOINT_VERSION
    if _CHECKPOINT_VERSION is not None:
        assert _CHECKPOINT_VERSION == value, \
            "checkpoint versions do not match"
    _CHECKPOINT_VERSION = value


def get_checkpoint_version():
    global _CHECKPOINT_VERSION
    return _CHECKPOINT_VERSION


def print_datetime(string):
    """Note that this call will sync across all ranks."""
    torch.distributed.barrier()
    time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print_rank_0('[' + string + '] datetime: {} '.format(time_str))

def load_checkpoint(model, load_arg='load', strict=True):
    """Load a model checkpoint and return the iteration.
    strict (bool): whether to strictly enforce that the keys in
        :attr:`state_dict` of the checkpoint match the names of
        parameters and buffers in model.
    """
    args = get_args()
    load_dir = getattr(args, load_arg)

    model = unwrap_model(model)

    state_dict, checkpoint_name, release = _load_base_checkpoint(load_dir, rank0=False)

    # Checkpoint not loaded.
    if state_dict is None:

        # Conditionally exit at this point.
        if args.exit_on_missing_checkpoint:
            print_rank_0(">> '--exit-on-missing-checkpoint' set ... exiting. <<")
            torch.distributed.barrier()
            sys.exit()

        # Iteration defaults to 0.
        return 0
    
    set_checkpoint_version(state_dict.get('checkpoint_version', 0))

    # Model.
    if len(model) == 1:
        model[0].load_state_dict(state_dict['model'], strict=strict)
    else:
        for i in range(len(model)):
            mpu.set_virtual_pipeline_model_parallel_rank(i)
            model[i].load_state_dict(state_dict['model%d' % i], strict=strict)

    # Fix up query/key/value matrix ordering if needed.
    checkpoint_version = get_checkpoint_version()
    print_rank_0(f' checkpoint version {checkpoint_version}')
    fix_query_key_value_ordering(model, checkpoint_version)


    return 0

    
def convert_hf(model_provider,
             model_type,
             extra_args_provider=None,
             args_defaults={}):
    """Main training program.

    This function will run the followings in the order provided:
        1) initialize Megatron.
        2) setup model, optimizer and lr schedule using the model_provider.
        3) call train_val_test_data_provider to get train/val/test datasets.
        4) train the modle using the forward_step_func.

    Arguments:
        model_provider: a function that returns a vanilla version of the
            model. By vanilla we mean a simple model on cpu with no fp16 or ddp.
        model_type: an enum that specifies the type of model being trained.
    """
    # Initalize and get arguments, timers, and Tensorboard writer.
    initialize_megatron(extra_args_provider=extra_args_provider,
                        args_defaults=args_defaults)
    # Set pytorch JIT layer fusion options and warmup JIT functions.
    set_jit_fusion_options()

    # Adjust the startup time so it reflects the largest value.
    # This will be closer to what scheduler will see (outside of
    # image ... launches.
    global _TRAIN_START_TIME
    start_time_tensor = torch.cuda.DoubleTensor([_TRAIN_START_TIME])
    torch.distributed.all_reduce(start_time_tensor,
                                 op=torch.distributed.ReduceOp.MIN)
    _TRAIN_START_TIME = start_time_tensor.item()
    print_rank_0('time to initialize megatron (seconds): {:.3f}'.format(
        time.time() - _TRAIN_START_TIME))
    print_datetime('after megatron is initialized')

    args = get_args()
    timers = get_timers()
    
    pp_rank = args.transformer_pipeline_model_parallel_size
    num_layers = args.num_layers
    save_path = args.save

    # Model, optimizer, and learning rate.
    timers('model-and-optimizer-setup', log_level=0).start(barrier=True)
    model = setup_model_and_optimizer(
        model_provider, model_type)
    timers('model-and-optimizer-setup').stop()
    print_datetime('after model, optimizer, and learning rate '
                   'scheduler are built')
    config = get_model_config(model[0])

    state_dict = model[0].state_dict() 
    
    config1 = transformers.AutoConfig.from_pretrained(args.load, trust_remote_code=True)
    config1.save_pretrained(args.save)

    from yuan_moe_hf_model import YuanForCausalLM
   
    #config1.hidden_size = config1.hidden_size//8
    #config1.intermediate_size = config1.intermediate_size//8
    #config1.moe_config['ffn_hidden_size'] = config1.moe_config['ffn_hidden_size']//8 

    #hf_model = YuanForCausalLM(config1)
    
    assert config1.num_hidden_layers == config.num_layers
    assert config1.hidden_size == config.hidden_size
    
     
    layer_num = config1.num_hidden_layers
    num_experts = config1.moe_config['moe_num_experts']
    intermediate_size = config1.intermediate_size
    hidden_size = config1.hidden_size
    
    config1.hidden_size = config1.hidden_size//8
    config1.intermediate_size = config1.intermediate_size//8
    config1.moe_config['ffn_hidden_size'] = config1.moe_config['ffn_hidden_size']//8
    hf_model = YuanForCausalLM(config1)



    hf_keys = []
    hf_state_dict = {}
    for i in hf_model.state_dict().keys():
        hf_keys.append(i)

    for hf_key in hf_keys:
        if 'embed_tokens.weight' in hf_key:
            try:
                hf_state_dict[hf_key] = state_dict['language_model.embedding.word_embeddings.weight']
            except:
                continue
        elif 'model.norm.weight' in hf_key:
            try:
                hf_state_dict[hf_key] = state_dict['language_model.encoder.final_layernorm.weight']
            except:
                continue
    try:
        hf_state_dict['lm_head.weight'] = state_dict['word_embeddings.weight']
    except:
        pass
    
    for layer_number in range(layer_num):
        keys_in_one_block = []
        for hf_key in hf_keys:
            if 'layers.'+str(layer_number)+'.' in hf_key:
                keys_in_one_block.append(hf_key)


        for hf_key in keys_in_one_block:
            front = 'language_model.encoder.layers.' + str(layer_number) + '.'

            # layer norm
            if "input_layernorm.weight" in hf_key:
                try:
                    hf_state_dict[hf_key] = state_dict[front + 'input_layernorm.weight']
                except:
                    continue

            # attention
            if "self_attn.v_proj" in hf_key:
                try:
                    hf_state_dict[hf_key] = state_dict[front + 'self_attention.get_value.weight']
                except:
                    continue

            if "lf_gate.conv1.weight" in hf_key:
                try:
                    hf_state_dict[hf_key] = state_dict[front + 'self_attention.lf_gate.conv1.weight']
                except:
                    continue

            if "lf_gate.conv1.bias" in hf_key:
                try:
                    hf_state_dict[hf_key] = state_dict[front + 'self_attention.lf_gate.conv1.bias']
                except:
                    continue

            if "lf_gate.conv2.weight" in hf_key:
                try:
                    hf_state_dict[hf_key] = state_dict[front + 'self_attention.lf_gate.conv2.weight']
                except:
                    continue

            if "lf_gate.conv2.bias" in hf_key:
                try:
                    hf_state_dict[hf_key] = state_dict[front + 'self_attention.lf_gate.conv2.bias']
                except:
                    continue

            if "self_attn.lf_gate.output_layernorm.weight" in hf_key:
                try:
                    hf_state_dict[hf_key] = state_dict[front + 'self_attention.lf_gate.output_layernorm.weight']
                except:
                    continue

            if "self_attn.o_proj.weight" in hf_key:
                try:
                    hf_state_dict[hf_key] = state_dict[front + 'self_attention.dense.weight']
                except:
                    continue

            if "post_attention_layernorm.weight" in hf_key:
                try:
                    hf_state_dict[hf_key] = state_dict[front + 'post_attention_layernorm.weight']
                except:
                    continue

            if "self_attn.q_proj.weight" in hf_key:
                try:
                    qk_tensor = state_dict[front + 'self_attention.get_query_key.weight']
                    hf_state_dict[hf_key] = torch.split(qk_tensor, split_size_or_sections=qk_tensor.size(0)//2, dim=0)[0]
                except:
                    continue

            if "self_attn.k_proj.weight" in hf_key:
                try:
                    qk_tensor = state_dict[front + 'self_attention.get_query_key.weight']
                    hf_state_dict[hf_key] = torch.split(qk_tensor, split_size_or_sections=qk_tensor.size(0)//2, dim=0)[1]         
                except:
                    continue

            # router gate
            if "mlp.gate.query.weight" in hf_key:
                try:
                    qkv_tensor = state_dict[front + 'mlp.router.attention_router.query_key_value.weight']
                    hf_state_dict[hf_key] = torch.split(qkv_tensor, split_size_or_sections=qkv_tensor.size(0)//3, dim=0)[0]
                except:
                    continue

            if "mlp.gate.key.weight" in hf_key:
                try:
                    qkv_tensor = state_dict[front + 'mlp.router.attention_router.query_key_value.weight']
                    hf_state_dict[hf_key] = torch.split(qkv_tensor, split_size_or_sections=qkv_tensor.size(0)//3, dim=0)[1]
                except:
                    continue

            if "mlp.gate.value.weight" in hf_key:
                try:
                    qkv_tensor = state_dict[front + 'mlp.router.attention_router.query_key_value.weight']
                    hf_state_dict[hf_key] = torch.split(qkv_tensor, split_size_or_sections=qkv_tensor.size(0)//3, dim=0)[2]
                except:
                    continue

            # experts
            if 'experts' in hf_key:
                experts_index = hf_key.find("experts.")
                start_index = experts_index + len("experts.")
                end_index = hf_key.find(".", start_index)
                experts_np = int(hf_key[start_index:end_index])
            
                if 'w1' in hf_key:
                    try:
                        mlp1_tensor = state_dict[front + 'mlp.experts.weight1']
                        mlp1_tensor = mlp1_tensor.view(num_experts, hidden_size, -1)

                        hf_state_dict[hf_key] = mlp1_tensor[experts_np].T    
                    except:
                        continue
                if 'w2' in hf_key:
                    try:
                        mlp2_tensor = state_dict[front + 'mlp.experts.weight2']
                        mlp2_tensor = mlp2_tensor.view(num_experts, -1, hidden_size)
                        hf_state_dict[hf_key] = mlp2_tensor[experts_np].T
                    except:
                        continue
    
    if pp_rank == 1:
        save_name = 'pytorch_model.bin'
        torch.save(hf_state_dict, save_path + '/' + save_name)

    if pp_rank > 1:
        print('rank',args.rank,hf_state_dict.keys())                
        save_name = 'pytorch_model_' + str(args.rank) + '.bin'
        torch.save(hf_state_dict, save_path + '/' + save_name)

    exit()
    return 0


def get_model(model_provider_func, model_type=ModelType.encoder_or_decoder, wrap_with_ddp=True):
    """Build the model."""
    args = get_args()
    args.model_type = model_type

    # Build model.
    if mpu.get_pipeline_model_parallel_world_size() > 1 and \
       args.virtual_pipeline_model_parallel_size is not None:
        assert model_type != ModelType.encoder_and_decoder, \
            "Interleaved schedule not supported for model with both encoder and decoder"
        model = []
        for i in range(args.virtual_pipeline_model_parallel_size):
            mpu.set_virtual_pipeline_model_parallel_rank(i)
            # Set pre_process and post_process only after virtual rank is set.
            pre_process = mpu.is_pipeline_first_stage()
            post_process = mpu.is_pipeline_last_stage()
            this_model = model_provider_func(
                pre_process=pre_process,
                post_process=post_process
            )
            this_model.model_type = model_type
            model.append(this_model)
    else:
        pre_process = mpu.is_pipeline_first_stage()
        post_process = mpu.is_pipeline_last_stage()
        add_encoder = True
        add_decoder = True
        if model_type == ModelType.encoder_and_decoder:
            if mpu.get_pipeline_model_parallel_world_size() > 1:
                assert args.pipeline_model_parallel_split_rank is not None, \
                    "Split rank needs to be specified for model with both encoder and decoder"
                rank = mpu.get_pipeline_model_parallel_rank()
                split_rank = args.pipeline_model_parallel_split_rank
                world_size = mpu.get_pipeline_model_parallel_world_size()
                pre_process = rank == 0 or rank == split_rank
                post_process = (rank == (split_rank - 1)) or (
                        rank == (world_size - 1))
                add_encoder = mpu.is_pipeline_stage_before_split()
                add_decoder = mpu.is_pipeline_stage_after_split()
            model = model_provider_func(
                pre_process=pre_process,
                post_process=post_process,
                add_encoder=add_encoder,
                add_decoder=add_decoder)
        else:
            model = model_provider_func(
                pre_process=pre_process,
                post_process=post_process
            )
        model.model_type = model_type

    if not isinstance(model, list):
        model = [model]

    # Disallow training and inference with Transformer Engine
    # for non-GPT models
    args.allow_transformer_engine = all([type(m) == GPTModel for m in model])
    assert args.allow_transformer_engine or args.transformer_impl == 'local', \
        'Transformer Engine is only approved for GPT models'

    # Set tensor model parallel attributes if not set.
    # Only parameters that are already tensor model parallel have these
    # attributes set for them. We should make sure the default attributes
    # are set for all params so the optimizer can use them.
    for model_module in model:
        for param in model_module.parameters():
            tensor_parallel.set_defaults_if_not_set_tensor_model_parallel_attributes(param)

    # Print number of parameters.
    if mpu.get_data_parallel_rank() == 0:
        print(' > number of parameters on (tensor, pipeline) '
              'model parallel rank ({}, {}): {}'.format(
            mpu.get_tensor_model_parallel_rank(),
            mpu.get_pipeline_model_parallel_rank(),
            sum([sum([p.nelement() for p in model_module.parameters()])
                 for model_module in model])), flush=True)

    # GPU allocation.
    for model_module in model:
        model_module.cuda(torch.cuda.current_device())

    # Fp16 conversion.
    if args.fp16 or args.bf16:
        model = [Float16Module(model_module, args) for model_module in model]
    """
    if wrap_with_ddp:
        if args.DDP_impl == 'torch':
            i = torch.cuda.current_device()
            model = [torchDDP(model_module, device_ids=[i], output_device=i,
                              process_group=mpu.get_data_parallel_group())
                     for model_module in model]

        elif args.DDP_impl == 'local':
            model = [LocalDDP(model_module,
                              args.accumulate_allreduce_grads_in_fp32,
                              args.use_contiguous_buffers_in_local_ddp)
                     for model_module in model]
            # broad cast params from data parallel src rank to other data parallel ranks
            if args.data_parallel_random_init:
                for model_module in model:
                    model_module.broadcast_params()
        else:
            raise NotImplementedError('Unknown DDP implementation specified: '
                                      '{}. Exiting.'.format(args.DDP_impl))
    """
    return model


def get_optimizer_param_scheduler(optimizer):
    """Build the learning rate scheduler."""
    args = get_args()

    # Iteration-based training.
    if args.train_iters:
        if args.lr_decay_iters is None:
            args.lr_decay_iters = args.train_iters
        lr_decay_steps = args.lr_decay_iters * args.global_batch_size
        wd_incr_steps = args.train_iters * args.global_batch_size
        if args.lr_warmup_fraction is not None:
            lr_warmup_steps = args.lr_warmup_fraction * lr_decay_steps
        else:
            lr_warmup_steps = args.lr_warmup_iters * args.global_batch_size
    # Sample-based training.
    elif args.train_samples:
        # We need to set training iters for later use. Technically
        # we need to adjust the training samples too (due to last
        # batch being incomplete) but we leave it as is for now.
        update_train_iters(args)
        if args.lr_decay_samples is None:
            args.lr_decay_samples = args.train_samples
        lr_decay_steps = args.lr_decay_samples
        wd_incr_steps = args.train_samples
        if args.lr_warmup_fraction is not None:
            lr_warmup_steps = args.lr_warmup_fraction * lr_decay_steps
        else:
            lr_warmup_steps = args.lr_warmup_samples
    else:
        raise Exception(
            'either train-iters or train-samples should be provided.')

    opt_param_scheduler = OptimizerParamScheduler(
        optimizer,
        max_lr=args.lr,
        min_lr=args.min_lr,
        lr_warmup_steps=lr_warmup_steps,
        lr_decay_steps=lr_decay_steps,
        lr_decay_style=args.lr_decay_style,
        start_wd=args.start_weight_decay,
        end_wd=args.end_weight_decay,
        wd_incr_steps=wd_incr_steps,
        wd_incr_style=args.weight_decay_incr_style,
        use_checkpoint_opt_param_scheduler=args.use_checkpoint_opt_param_scheduler,
        override_opt_param_scheduler=args.override_opt_param_scheduler)

    return opt_param_scheduler


def setup_model_and_optimizer(model_provider_func,
                              model_type,
                              no_wd_decay_cond=None,
                              scale_lr_cond=None,
                              lr_mult=1.0):
    """Setup model and optimizer."""
    args = get_args()

    model = get_model(model_provider_func, model_type)
    #unwrapped_model = unwrap_model(model,
    #                               (torchDDP, LocalDDP, Float16Module))

    if args.load is not None:
        timers = get_timers()
        timers('load-checkpoint', log_level=0).start(barrier=True)
        args.iteration = load_checkpoint(model)
        timers('load-checkpoint').stop(barrier=True)
        timers.log(['load-checkpoint'])
    else:
        args.iteration = 0

    # We only support local DDP with multiple micro-batches.
    if len(model) > 1 or mpu.get_pipeline_model_parallel_world_size() > 1:
        assert args.DDP_impl == 'local'

    # get model without FP16 and/or TorchDDP wrappers
    #if args.iteration == 0 and len(unwrapped_model) == 1 \
    #    and hasattr(unwrapped_model[0], 'init_state_dict_from_bert'):
    #    print_rank_0("Initializing ICT from pretrained BERT model")
    #    unwrapped_model[0].init_state_dict_from_bert()

    return model


def save_checkpoint_and_time(iteration, model, optimizer, opt_param_scheduler):
    timers = get_timers()
    # Extra barrier is added to make sure
    # all ranks report the max time.
    timers('save-checkpoint', log_level=0).start(barrier=True)
    save_checkpoint(iteration, model, optimizer, opt_param_scheduler)
    timers('save-checkpoint').stop(barrier=True)
    timers.log(['save-checkpoint'])


def model_provider(pre_process=True, post_process=True):
    """Build the model."""

    print_rank_0('building GPT model ...')
    config = core_transformer_config_from_args(get_args())
    
    #model = GPTModel(
    model = YuanModel(
        config,
        num_tokentypes=0,
        parallel_output=True,
        pre_process=pre_process,
        post_process=post_process
    )
    return model


if __name__ == "__main__":
     convert_hf(model_provider,
                ModelType.encoder_or_decoder
               )

