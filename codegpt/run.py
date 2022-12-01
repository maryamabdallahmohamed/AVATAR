# coding=utf-8
# Copyright 2018 The Google AI Language Team Authors and The HuggingFace Inc. team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
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
"""
Text to code generation pipeline in CodeXGLUE
"""

from __future__ import absolute_import, division, print_function

import sys

sys.path.append("..")

import os
import argparse
import logging
import random
from tqdm import tqdm

import numpy as np
import torch
from torch.utils.data import DataLoader, SequentialSampler, RandomSampler
from torch.nn import CrossEntropyLoss

from evaluation.bleu import _bleu
from codegpt.dataset import ConcodeDataset, read_data
from codegpt.beam import Beam

from transformers import (
    WEIGHTS_NAME,
    AdamW,
    get_linear_schedule_with_warmup,
    BertConfig,
    BertForMaskedLM,
    BertTokenizer,
    GPT2Config,
    GPT2LMHeadModel,
    GPT2Tokenizer,
    OpenAIGPTConfig,
    OpenAIGPTLMHeadModel,
    OpenAIGPTTokenizer,
    RobertaConfig,
    RobertaForMaskedLM,
    RobertaTokenizer,
    DistilBertConfig,
    DistilBertForMaskedLM,
    DistilBertTokenizer
)

try:
    from torch.utils.tensorboard import SummaryWriter
except:
    from tensorboardX import SummaryWriter

logger = logging.getLogger(__name__)

MODEL_CLASSES = {
    'gpt2': (GPT2Config, GPT2LMHeadModel, GPT2Tokenizer),
    'openai-gpt': (OpenAIGPTConfig, OpenAIGPTLMHeadModel, OpenAIGPTTokenizer),
    'bert': (BertConfig, BertForMaskedLM, BertTokenizer),
    'roberta': (RobertaConfig, RobertaForMaskedLM, RobertaTokenizer),
    'distilbert': (DistilBertConfig, DistilBertForMaskedLM, DistilBertTokenizer)
}


def load_and_cache_examples(args, tokenizer, evaluate=False):
    dataset = ConcodeDataset(
        tokenizer, args, logger,
        file_type='valid' if evaluate else 'train',
        block_size=args.block_size
    )
    return dataset


def set_seed(args):
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if args.n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)


def update_config(model, tokenizer):
    model.config.bos_token_id = tokenizer.bos_token_id
    model.config.eos_token_id = tokenizer.eos_token_id
    model.config.pad_token_id = tokenizer.pad_token_id


def train(args, train_dataset, model, tokenizer, fh, pool):
    """ Train the model """
    if args.local_rank in [-1, 0]:
        args.tensorboard_dir = os.path.join(args.output_dir, 'tensorboard')
        if not os.path.exists(args.tensorboard_dir):
            os.makedirs(args.tensorboard_dir)
        tb_writer = SummaryWriter(args.tensorboard_dir)

    args.batch_size = args.per_gpu_train_batch_size * max(1, args.n_gpu)
    train_sampler = RandomSampler(train_dataset)

    train_dataloader = DataLoader(
        train_dataset,
        sampler=train_sampler,
        batch_size=args.batch_size,
        drop_last=True
    )
    total_examples = len(train_dataset) * (
        torch.distributed.get_world_size() if args.local_rank != -1 else 1)
    batch_size = args.batch_size * args.gradient_accumulation_steps * (
        torch.distributed.get_world_size() if args.local_rank != -1 else 1)

    if args.max_steps > 0:
        t_total = args.max_steps
        args.num_train_epochs = args.max_steps // (len(train_dataloader) // args.gradient_accumulation_steps) + 1
    else:
        t_total = len(train_dataloader) // args.gradient_accumulation_steps * args.num_train_epochs

    model.to(args.device)
    if args.local_rank not in [-1, 0]:
        torch.distributed.barrier()
        # Prepare optimizer and schedule (linear warmup and decay)
    no_decay = ['bias', 'LayerNorm.weight']
    optimizer_grouped_parameters = [
        {'params': [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
         'weight_decay': args.weight_decay},
        {'params': [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
    ]
    optimizer = AdamW(optimizer_grouped_parameters, lr=args.learning_rate, eps=args.adam_epsilon)
    scheduler = get_linear_schedule_with_warmup(
        optimizer,
        num_warmup_steps=args.warmup_steps,
        num_training_steps=t_total
    )
    checkpoint_last = os.path.join(args.output_dir, 'checkpoint-last')
    scheduler_last = os.path.join(checkpoint_last, 'scheduler.pt')
    optimizer_last = os.path.join(checkpoint_last, 'optimizer.pt')
    if os.path.exists(scheduler_last):
        scheduler.load_state_dict(torch.load(scheduler_last, map_location="cpu"))
    if os.path.exists(optimizer_last):
        optimizer.load_state_dict(torch.load(optimizer_last, map_location="cpu"))
    if args.local_rank == 0:
        torch.distributed.barrier()
    if args.fp16:
        try:
            from apex import amp
        except ImportError:
            raise ImportError("Please install apex from https://www.github.com/nvidia/apex to use fp16 training.")
        model, optimizer = amp.initialize(model, optimizer, opt_level=args.fp16_opt_level)

    # multi-gpu training (should be after apex fp16 initialization)
    if args.n_gpu > 1:
        model = torch.nn.DataParallel(model)

    # Distributed training (should be after apex fp16 initialization)
    if args.local_rank != -1:
        model = torch.nn.parallel.DistributedDataParallel(
            model, device_ids=[args.local_rank % args.gpu_per_node],
            output_device=args.local_rank % args.gpu_per_node,
            find_unused_parameters=True
        )

    # Train!
    logger.info("***** Running training *****")
    logger.info("  Num examples = %d", total_examples)
    logger.info("  Num epoch = %d", t_total * batch_size // total_examples)
    logger.info("  Instantaneous batch size per GPU = %d", args.per_gpu_train_batch_size)
    logger.info("  Total train batch size (w. parallel, distributed & accumulation) = %d", batch_size)
    logger.info("  Gradient Accumulation steps = %d", args.gradient_accumulation_steps)
    logger.info("  Total optimization steps = %d", t_total)

    global_step = args.start_step
    tr_loss, logging_loss, avg_loss, tr_nb = 0.0, 0.0, 0.0, 0
    # model.resize_token_embeddings(len(tokenizer))
    model.zero_grad()
    set_seed(args)  # Added here for reproducibility (even between python 2 and 3)

    best_bleu, best_loss = 0.0, 1e6
    patience_counter = 0
    for idx in range(args.start_epoch, int(args.num_train_epochs)):
        train_dataloader = tqdm(train_dataloader, total=len(train_dataloader),
                                desc='Global Step %6d AvgLoss %3.2f' % (global_step, 0.))
        for step, (batch, token_labels) in enumerate(train_dataloader):
            inputs = batch.to(args.device)
            attn_mask = torch.tensor(token_labels.clone().detach() != 0, dtype=torch.uint8, device=args.device)
            loss_mask = torch.tensor(token_labels.clone().detach() == 2, dtype=torch.uint8, device=args.device)
            model.train()
            outputs = model(inputs, attention_mask=attn_mask)
            logits = outputs[0]
            labels = inputs
            shift_logits = logits[..., :-1, :].contiguous()
            shift_labels = labels[..., 1:].contiguous()
            # Flatten the tokens
            loss_fct = CrossEntropyLoss()
            flatten_shift_loss_mask = loss_mask[..., :-1].contiguous().view(-1)
            ids = torch.nonzero(flatten_shift_loss_mask).view(-1)
            loss = loss_fct(shift_logits.view(-1, shift_logits.size(-1))[ids], shift_labels.view(-1)[ids])

            if args.n_gpu > 1:
                loss = loss.mean()  # mean() to average on multi-gpu parallel training
            if args.gradient_accumulation_steps > 1:
                loss = loss / args.gradient_accumulation_steps

            if args.fp16:
                with amp.scale_loss(loss, optimizer) as scaled_loss:
                    scaled_loss.backward()
                torch.nn.utils.clip_grad_norm_(amp.master_params(optimizer), args.max_grad_norm)
            else:
                loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), args.max_grad_norm)

            tr_loss += loss.item()

            if (step + 1) % args.gradient_accumulation_steps == 0:
                optimizer.step()
                optimizer.zero_grad()
                scheduler.step()
                global_step += 1
                avg_loss = round(np.exp((tr_loss - logging_loss) / (global_step - tr_nb)), 4)
                train_dataloader.set_description('Global Step %6d AvgLoss %3.2f' % (global_step, avg_loss))
                if args.local_rank in [-1, 0] and args.logging_steps > 0 and global_step % args.logging_steps == 0:
                    # Log metrics
                    tb_writer.add_scalar('lr', scheduler.get_last_lr()[0], global_step)
                    tb_writer.add_scalar('loss', (tr_loss - logging_loss) / args.logging_steps, global_step)
                    logging_loss = tr_loss
                    tr_nb = global_step

            if args.max_steps > 0 and global_step > args.max_steps:
                break

        model_to_save = (
            model.module if hasattr(model, "module") else model
        )

        # dev_bleu, dev_EM = eval_bleu(args, model, tokenizer, file_type='valid', max_example=500)
        # logger.info(f"dev bleu: {dev_bleu}, dev EM: {dev_EM}")

        # output_dir = os.path.join(args.output_dir, 'checkpoint-best-bleu')
        # if not os.path.exists(output_dir):
        #     os.makedirs(output_dir)
        #
        # if dev_bleu > best_bleu:
        #     best_bleu = dev_bleu
        #     logger.info(f"best bleu updated. saved in {output_dir}")
        #     logger.info(f"best bleu: {best_bleu}")
        #     model_to_save.save_pretrained(output_dir)
        #     tokenizer.save_pretrained(output_dir)
        #     torch.save(args, os.path.join(output_dir, "training_args.bin"))
        #     patience_counter = 0
        # else:
        #     patience_counter += 1
        #     logger.info('Hitting patience counter : %d' % patience_counter)

        dev_result = evaluate(args, model, tokenizer)
        ppl = dev_result["perplexity"]
        logger.info(f"dev ppl: {ppl}")

        output_dir = os.path.join(args.output_dir, 'checkpoint-best-ppl')
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        if dev_result["loss"] < best_loss:
            logger.info("  Best ppl:%s", round(dev_result["perplexity"], 5))
            logger.info("  " + "*" * 20)
            best_loss = dev_result["loss"]
            model_to_save.save_pretrained(output_dir)
            tokenizer.save_pretrained(output_dir)
            torch.save(args, os.path.join(output_dir, "training_args.bin"))
            patience_counter = 0
        else:
            patience_counter += 1
            logger.info('Hitting patience counter : %d' % patience_counter)

        last_output_dir = os.path.join(args.output_dir, 'checkpoint-last')
        logger.info("Saving model checkpoint to %s", last_output_dir)
        if not os.path.exists(last_output_dir):
            os.makedirs(last_output_dir)
        torch.save(args, os.path.join(last_output_dir, "training_args.bin"))
        model_to_save.save_pretrained(last_output_dir)
        tokenizer.save_pretrained(last_output_dir)

        idx_file = os.path.join(last_output_dir, 'idx_file.txt')
        with open(idx_file, 'w', encoding='utf-8') as idxf:
            idxf.write(str(0) + '\n')
        torch.save(optimizer.state_dict(), os.path.join(last_output_dir, "optimizer.pt"))
        torch.save(scheduler.state_dict(), os.path.join(last_output_dir, "scheduler.pt"))
        logger.info("Saving optimizer and scheduler states to %s", last_output_dir)

        step_file = os.path.join(last_output_dir, 'step_file.txt')
        with open(step_file, 'w', encoding='utf-8') as stepf:
            stepf.write(str(global_step) + '\n')

        if patience_counter >= args.max_patience:
            logger.info('Exiting training!')
            break

    if args.local_rank in [-1, 0]:
        tb_writer.close()

    return global_step, tr_loss / global_step


def evaluate(args, model, tokenizer, prefix="", eval_when_training=False):
    # Loop to handle MNLI double evaluation (matched, mis-matched)
    eval_output_dir = args.output_dir

    eval_dataset = load_and_cache_examples(args, tokenizer, evaluate=True)

    if not os.path.exists(eval_output_dir) and args.local_rank in [-1, 0]:
        os.makedirs(eval_output_dir)

    args.eval_batch_size = args.per_gpu_eval_batch_size * max(1, args.n_gpu)
    # Note that DistributedSampler samples randomly
    eval_sampler = SequentialSampler(eval_dataset)
    eval_dataloader = DataLoader(eval_dataset, sampler=eval_sampler, batch_size=args.eval_batch_size)

    eval_loss = 0.0
    nb_eval_steps = 0
    model.eval()
    for step, (batch, token_labels) in enumerate(eval_dataloader):
        inputs = batch.to(args.device)
        attn_mask = torch.tensor(token_labels.clone().detach() != 0, dtype=torch.uint8, device=args.device)
        loss_mask = torch.tensor(token_labels.clone().detach() == 2, dtype=torch.uint8, device=args.device)
        with torch.no_grad():
            outputs = model(inputs, attention_mask=attn_mask)
            logits = outputs[0]
            labels = inputs
            shift_logits = logits[..., :-1, :].contiguous()
            shift_labels = labels[..., 1:].contiguous()
            # Flatten the tokens
            loss_fct = CrossEntropyLoss()
            flatten_shift_loss_mask = loss_mask[..., :-1].contiguous().view(-1)
            ids = torch.nonzero(flatten_shift_loss_mask).view(-1)
            loss = loss_fct(shift_logits.view(-1, shift_logits.size(-1))[ids], shift_labels.view(-1)[ids])
            eval_loss += loss.mean().item()
        nb_eval_steps += 1

    eval_loss = eval_loss / nb_eval_steps
    perplexity = torch.exp(torch.tensor(eval_loss))

    result = {
        "loss": eval_loss,
        "perplexity": float(perplexity)
    }

    # output_eval_file = os.path.join(eval_output_dir, prefix, "eval_results.txt")
    # with open(output_eval_file, "w") as writer:
    #     for key in sorted(result.keys()):
    #         writer.write("%s = %s\n" % (key, str(result[key])))

    return result


def eval_bleu(args, model, tokenizer, file_type='test', max_example=999999999):
    dataset = ConcodeDataset(
        tokenizer, args, logger,
        file_type=file_type,
        block_size=args.block_size,
        mode='test',
        max_example=max_example
    )
    test_sampler = SequentialSampler(dataset)
    test_dataloader = DataLoader(dataset, sampler=test_sampler, batch_size=1)
    model.to(args.device)
    model.zero_grad()
    model.eval()

    pred_ids = []
    for batch in tqdm(test_dataloader, total=len(test_dataloader), desc="Eval bleu"):
        source_ids = batch[0].to(args.device)
        with torch.no_grad():
            preds = model.generate(source_ids,
                                   use_cache=True,
                                   num_beams=args.beam_size,
                                   max_length=args.block_size)
            top_preds = list(preds[:, source_ids.size(1):].cpu().numpy())
            pred_ids.extend(top_preds)

    preds = [tokenizer.decode(id, skip_special_tokens=True, clean_up_tokenization_spaces=False) for id in pred_ids]

    golds = []
    datas = read_data(args.data_dir, args.source, args.target, file_type)
    for x in datas:
        golds.append(x["code"])

    assert len(preds) == len(golds)

    EM = []
    with open(os.path.join(args.output_dir, f"{file_type}.output"), 'w', encoding='utf-8') as f, \
            open(os.path.join(args.output_dir, f"{file_type}.gold"), 'w', encoding='utf-8') as f1:
        for pred, gold in zip(preds, golds):
            f.write(pred + '\n')
            f1.write(gold + '\n')
            EM.append(pred.split() == gold.split())
    bleu_score = round(_bleu(
        os.path.join(args.output_dir, f"{file_type}.gold"),
        os.path.join(args.output_dir, f"{file_type}.output")
    ), 2)
    EM = round(np.mean(EM) * 100, 2)
    return bleu_score, EM


def main():
    parser = argparse.ArgumentParser()

    ## Required parameters
    parser.add_argument("--data_dir", default=None, type=str, required=True,
                        help="The input data path.")
    parser.add_argument('--source', type=str, required=True)
    parser.add_argument('--target', type=str, required=True)
    parser.add_argument("--output_dir", default=None, type=str, required=True,
                        help="The output directory where the model predictions and checkpoints will be written.")
    parser.add_argument("--load_model_path", default=None, type=str,
                        help="Path to trained model: Should contain the .bin files")

    ## Other parameters
    parser.add_argument("--model_type", default="gpt2", type=str,
                        help="The model architecture to be fine-tuned.")
    parser.add_argument("--pretrain_dir", default="", type=str,
                        help="The output directory where the model predictions and checkpoints will be written.")
    parser.add_argument("--config_dir", type=str,
                        help="config name. Required when training from scratch")
    parser.add_argument("--tokenizer_dir", type=str,
                        help="Pre-trained tokenizer dir. Required when training from scratch")
    parser.add_argument("--load_name", type=str, default="pretrained",
                        help="Load pretrained model name")

    parser.add_argument("--mlm", action='store_true',
                        help="Train with masked-language modeling loss instead of language modeling.")
    parser.add_argument("--mlm_probability", type=float, default=0.15,
                        help="Ratio of tokens to mask for masked language modeling loss")

    parser.add_argument("--cache_dir", default="", type=str,
                        help="Optional directory to store the pre-trained models downloaded from s3 (instread of the default one)")
    parser.add_argument("--block_size", default=1024, type=int,
                        help="Optional input sequence length after tokenization."
                             "The training dataset will be truncated in block of this size for training."
                             "Default to the model max input length for single sentence inputs (take into account special tokens).")
    parser.add_argument("--do_train", action='store_true',
                        help="Whether to run training.")
    parser.add_argument("--do_eval", action='store_true',
                        help="Whether to run eval on the dev set.")
    parser.add_argument("--do_infer", action='store_true',
                        help="Whether to run inference on test set.")
    parser.add_argument("--evaluate_during_training", action='store_true',
                        help="Run evaluation during training at each logging step.")
    parser.add_argument("--do_lower_case", action='store_true',
                        help="Set this flag if you are using an uncased model.")

    parser.add_argument("--per_gpu_train_batch_size", default=2, type=int,
                        help="Batch size per GPU/CPU for training.")
    parser.add_argument("--per_gpu_eval_batch_size", default=4, type=int,
                        help="Batch size per GPU/CPU for evaluation.")
    parser.add_argument('--gradient_accumulation_steps', type=int, default=1,
                        help="Number of updates steps to accumulate before performing a backward/update pass.")
    parser.add_argument("--learning_rate", default=5e-5, type=float,
                        help="The initial learning rate for Adam.")
    parser.add_argument("--weight_decay", default=0.0, type=float,
                        help="Weight deay if we apply some.")
    parser.add_argument("--adam_epsilon", default=1e-8, type=float,
                        help="Epsilon for Adam optimizer.")
    parser.add_argument("--max_grad_norm", default=1.0, type=float,
                        help="Max gradient norm.")
    parser.add_argument("--num_train_epochs", default=1.0, type=float,
                        help="Total number of training epochs to perform.")
    parser.add_argument("--max_steps", default=-1, type=int,
                        help="If > 0: set total number of training steps to perform. Override num_train_epochs.")
    parser.add_argument("--warmup_steps", default=0, type=int,
                        help="Linear warmup over warmup_steps.")

    parser.add_argument('--logging_steps', type=int, default=10,
                        help="Log every X updates steps.")
    parser.add_argument('--save_steps', type=int, default=50,
                        help="Save checkpoint every X updates steps.")
    parser.add_argument('--save_total_limit', type=int, default=None,
                        help='Limit the total amount of checkpoints, delete the older checkpoints in the output_dir, does not delete by default')
    parser.add_argument("--eval_all_checkpoints", action='store_true',
                        help="Evaluate all checkpoints starting with the same prefix as model_name_or_path ending and ending with step number")
    parser.add_argument("--no_cuda", action='store_true',
                        help="Avoid using CUDA when available")
    parser.add_argument('--overwrite_output_dir', action='store_true',
                        help="Overwrite the content of the output directory")
    parser.add_argument('--overwrite_cache', action='store_true',
                        help="Overwrite the cached training and evaluation sets")
    parser.add_argument('--seed', type=int, default=42,
                        help="random seed for initialization")

    parser.add_argument('--fp16', action='store_true',
                        help="Whether to use 16-bit (mixed) precision (through NVIDIA apex) instead of 32-bit")
    parser.add_argument('--fp16_opt_level', type=str, default='O1',
                        help="For fp16: Apex AMP optimization level selected in ['O0', 'O1', 'O2', and 'O3']."
                             "See details at https://nvidia.github.io/apex/amp.html")
    parser.add_argument("--local_rank", type=int, default=-1,
                        help="For distributed training: local_rank")
    parser.add_argument("--node_index", type=int, default=-1,
                        help="node index if multi-node running")
    parser.add_argument("--gpu_per_node", type=int, default=-1,
                        help="num of gpus per node")
    parser.add_argument('--server_ip', type=str, default='', help="For distant debugging.")
    parser.add_argument('--server_port', type=str, default='', help="For distant debugging.")

    parser.add_argument('--log_file', type=str, default=None)
    parser.add_argument('--tensorboard_dir', type=str)
    parser.add_argument('--max_patience', type=int, default=5)
    parser.add_argument('--beam_size', type=int, default=5)

    pool = None
    args = parser.parse_args()

    # args.output_dir = os.path.join(args.output_dir, args.dataset)

    if args.model_type in ["bert", "roberta", "distilbert"] and not args.mlm:
        raise ValueError("BERT and RoBERTa do not have LM heads but masked LM heads. They must be run using the --mlm "
                         "flag (masked language modeling).")

    if os.path.exists(args.output_dir) and os.listdir(
            args.output_dir) and args.do_train and not args.overwrite_output_dir:
        raise ValueError(
            "Output directory ({}) already exists and is not empty. Use --overwrite_output_dir to overcome.".format(
                args.output_dir))

    # Setup distant debugging if needed
    if args.server_ip and args.server_port:
        # Distant debugging - see https://code.visualstudio.com/docs/python/debugging#_attach-to-a-local-script
        import ptvsd
        print("Waiting for debugger attach")
        ptvsd.enable_attach(address=(args.server_ip, args.server_port), redirect_output=True)
        ptvsd.wait_for_attach()

    logger.warning(
        "local_rank: %d, node_index: %d, gpu_per_node: %d" % (args.local_rank, args.node_index, args.gpu_per_node))

    # Setup CUDA, GPU & distributed training
    if args.local_rank == -1 or args.no_cuda:
        device = torch.device("cuda:0" if torch.cuda.is_available() and not args.no_cuda else "cpu")
        args.n_gpu = torch.cuda.device_count()
    else:  # Initializes the distributed backend which will take care of sychronizing nodes/GPUs
        torch.cuda.set_device(args.local_rank)
        device = torch.device("cuda", args.local_rank)
        torch.distributed.init_process_group(backend='nccl')
        args.local_rank += args.node_index * args.gpu_per_node
        args.n_gpu = 1
    args.device = device

    # Setup logging
    logging.basicConfig(format='%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                        datefmt='%m/%d/%Y %H:%M:%S',
                        level=logging.INFO if args.local_rank in [-1, 0] else logging.WARN)
    logger.warning(
        "Process rank: %s, device: %s, n_gpu: %s, distributed training: %s, 16-bits training: %s, world size: %s",
        args.local_rank, device, args.n_gpu, bool(args.local_rank != -1), args.fp16,
        torch.distributed.get_world_size() if args.local_rank != -1 else 1
    )

    # 使用FileHandler输出到文件
    fh = None
    if args.log_file is not None:
        fh = logging.FileHandler(args.log_file)
        logger.addHandler(fh)

    # Set seed
    set_seed(args)

    # Load pretrained model and tokenizer
    if args.local_rank not in [-1, 0]:
        torch.distributed.barrier()  # Barrier to make sure only the first process in distributed training download model & vocab

    args.start_epoch = 0
    args.start_step = 0
    checkpoint_last = os.path.join(args.output_dir, 'checkpoint-last')
    if args.do_train and os.path.exists(checkpoint_last) and os.listdir(checkpoint_last):
        args.pretrain_dir = os.path.join(checkpoint_last)
        args.config_name = os.path.join(checkpoint_last, 'config.json')
        idx_file = os.path.join(checkpoint_last, 'idx_file.txt')
        with open(idx_file, encoding='utf-8') as idxf:
            args.start_epoch = int(idxf.readlines()[0].strip()) + 1

        step_file = os.path.join(checkpoint_last, 'step_file.txt')
        if os.path.exists(step_file):
            with open(step_file, encoding='utf-8') as stepf:
                args.start_step = int(stepf.readlines()[0].strip())

        logger.info("reload model from {}, resume from {} epoch".format(checkpoint_last, args.start_epoch))

    # Load pre-trained model
    config_class, model_class, tokenizer_class = MODEL_CLASSES[args.model_type]
    pretrained = args.pretrain_dir
    if pretrained:
        tokenizer = tokenizer_class.from_pretrained(
            pretrained, do_lower_case=args.do_lower_case, bos_token='<s>',
            eos_token='</s>', pad_token='<pad>', unk_token='<|UNKNOWN|>', sep_token='<|SEP|>'
        )
        logger.info(tokenizer.encode("<s> hello world <pad> </s>"))
        model = model_class.from_pretrained(pretrained)
        model.resize_token_embeddings(len(tokenizer))
        update_config(model, tokenizer)
        logger.info(model.config)
    else:
        tokenizer = tokenizer_class.from_pretrained(
            args.tokenizer_dir, bos_token='<s>', eos_token='</s>',
            pad_token='<pad>', unk_token='<|UNKNOWN|>', sep_token='<|SEP|>'
        )
        args.vocab_size = tokenizer.vocab_size
        config = config_class.from_pretrained(args.config_dir)
        model = model_class(config)
        model.resize_token_embeddings(len(tokenizer))
        update_config(model, tokenizer)

    if args.load_model_path is not None:
        logger.info("reload model from {}".format(args.load_model_path))
        model.load_state_dict(torch.load(args.load_model_path))

    ###### Count and print number of parameters ######
    total_params = sum(p.numel() for p in model.parameters())
    total_trainable_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    logger.info("num. model params: {} (num. trained: {})".format(total_params, total_trainable_params))

    if args.local_rank == 0:
        torch.distributed.barrier()  # End of barrier to make sure only the first process in distributed training download model & vocab

    logger.info("Training/evaluation parameters %s", args)

    # Training
    if args.do_train:
        train_dataset = load_and_cache_examples(args, tokenizer, evaluate=False)
        global_step, tr_loss = train(args, train_dataset, model, tokenizer, fh, pool)
        logger.info(" global_step = %s, average loss = %s", global_step, tr_loss)

    if args.do_eval:  # only works on 1 GPU
        dev_bleu, dev_EM = eval_bleu(args, model, tokenizer, file_type='valid', max_example=2000)
        logger.info(f"dev bleu: {dev_bleu}, dev EM: {dev_EM}")

    if args.do_infer:  # only works on 1 GPU
        test_bleu, test_EM = eval_bleu(args, model, tokenizer, file_type='test', max_example=2000)
        logger.info(f"test bleu: {test_bleu}, test EM: {test_EM}")


if __name__ == "__main__":
    main()
