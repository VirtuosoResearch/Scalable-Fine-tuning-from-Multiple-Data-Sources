import argparse
import logging
import os

from src.custom.data_module import DataModule
from src.data.completion_dataset import CompletionMetadata

os.environ["TOKENIZERS_PARALLELISM"] = "false"

import pytorch_lightning as pl
import torch
from transformers import T5TokenizerFast, T5ForConditionalGeneration
from transformers import AutoModelForSeq2SeqLM, AutoTokenizer, BitsAndBytesConfig, AutoModelForCausalLM
from peft import get_peft_model, LoraConfig

from src.custom.model import Model
from torch.utils.data import DataLoader
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

import json
from evaluation.evaluator import Evaluator
from evaluation.summary import summarize_evaluation
import time

logging.basicConfig(level=logging.INFO)
torch.set_float32_matmul_precision("high")


def generate_state_dict(model, state_dict, coef, device, removing_keys = ["shared", "lm_head", "wte", "wpe", "ln", "layer_norm", "embed_tokens", "norm"]):
    # reshape coef
    new_state_dict = {}; cur_len = 0
    for key, param in model.named_parameters():
        if not param.requires_grad: 
            continue
        if any([rkey in key for rkey in removing_keys]):
            continue
        else:
            param_len = param.numel()
            new_state_dict[key] = state_dict[key].clone().to(device) + \
                torch.FloatTensor(coef[cur_len:cur_len+param_len].reshape(param.shape)).to(device)
            cur_len += param_len
    return new_state_dict

def compute_norm(state_dict, use_lora = False, remove_keys = ["shared", "lm_head", "wte", "wpe", "ln", "layer_norm", "embed_tokens", "norm"]):
    norm = 0
    for key, val in state_dict.items():
        if use_lora:
            if "lora" in key:
                norm += val.clone().square().sum().item()
        else:
            if any([rkey in key for rkey in remove_keys]):
                continue
            norm += val.clone().square().sum().item()
    return np.math.sqrt(norm)

def add_result_to_csv(result_datapoint, file_name):
    for key, val in result_datapoint.items():
        result_datapoint[key] = [val, ]
    
    if os.path.exists(file_name):
        result_df = pd.read_csv(file_name, index_col=0)
        tmp_df = pd.DataFrame(result_datapoint)
        result_df = pd.concat([result_df, tmp_df], ignore_index = True)
        result_df.to_csv(file_name)
    else:
        result_df = pd.DataFrame(result_datapoint)  
        result_df.to_csv(file_name)   

def evaluate(outputs, model, tokenizer):
    """
    Gather outputs from all GPUs and save validation predictions as a CompletionDataset and
    log validation metrics.

    Note, `all_gather` *concatenates* tensors from all GPUs along the first dimension.
    """
    # Determine total sample count and local max input/output length
    local_max_output_length = 0
    local_max_input_length = 0
    total_samples = 0
    for batch in outputs:
        local_max_input_length = max(local_max_input_length, batch["input"].shape[-1])
        local_max_output_length = max(local_max_output_length, batch["output"].shape[-1])
        total_samples += batch["sample_index"].shape[0]

    max_input_length = local_max_input_length
    max_output_length = local_max_output_length
    # Create local padded tensors
    local_outputs: dict = {
        "sample_index": torch.ones((total_samples,), dtype=torch.long) * tokenizer.pad_token_id,
        "input": torch.ones((total_samples, max_input_length), dtype=torch.long) * tokenizer.pad_token_id,
        "output": torch.ones((total_samples, max_output_length), dtype=torch.long) * tokenizer.pad_token_id,
    }

    # Populate local tensors
    start_index = 0
    for i, batch in enumerate(outputs):
        batch_size = batch["sample_index"].shape[0]
        end_index = start_index + batch_size
        local_outputs["sample_index"][start_index:end_index] = batch["sample_index"]
        input_width = batch["input"].shape[-1]
        output_width = batch["output"].shape[-1]
        if model.model_type == "encoder_decoder":
            local_outputs["input"][start_index:end_index, :input_width] = batch["input"]
            local_outputs["output"][start_index:end_index, :output_width] = batch["output"]
        elif model.model_type == "decoder":
            output_only_width = output_width - input_width
            local_outputs["input"][start_index:end_index, :input_width] = batch["input"]
            local_outputs["output"][start_index:end_index, :output_only_width] = batch["output"][:, input_width:]
        else:
            raise NotImplementedError("model_type='{}' not supported".format(model.model_type))

        start_index = end_index

    global_outputs = local_outputs
    if model.global_rank == 0:
        if global_outputs["sample_index"].dim() == 2:  # world_size > 1
            global_outputs["sample_index"] = global_outputs["sample_index"].flatten(start_dim=0, end_dim=1)
            global_outputs["output"] = global_outputs["output"].flatten(start_dim=0, end_dim=1)
            global_outputs["input"] = global_outputs["input"].flatten(start_dim=0, end_dim=1)

        final_output = {
            "sample_index": global_outputs["sample_index"].tolist(),
            "input": tokenizer.batch_decode(global_outputs["input"], skip_special_tokens=True),
            "output": tokenizer.batch_decode(global_outputs["output"], skip_special_tokens=True),
        }

        assert model.completion_metadata is not None
        # Save outputs as CompletionDataset
        cd = model._generate_completion_dataset(model.completion_metadata, final_output)
        cd.save()

        # Log metrics
        evaluation = Evaluator.evaluate_completion_dataset(cd)
        summary = summarize_evaluation(evaluation)
    return summary


def evaluate_subset(args, lm, tokenizer, data_loader, data_idxes, state_dict, device, projection_matrix, scale):
    # collect gradients for the subset
    gradient_dir = f"./gradients/{args.dataset_key}_{args.model_key}_{args.preset_key}_{args.project_dim}/run_{args.run}"
    gradients = []
    for idx in data_idxes:
        gradient_file_idx = idx // 8
        gradient_file = f"{gradient_dir}/train_batch_{gradient_file_idx}_gradients.npy"
        if not os.path.exists(gradient_file):
            print("File not found", gradient_file)
            continue
        tmp_gradients = np.load(gradient_file)
        gradients.append(tmp_gradients[idx % 8])
    gradients = np.array(gradients)
    if len(gradients) == 0:
        return None
    
    # randomly assign labels as 0 or 1
    labels = np.random.binomial(n=1, p=0.7, size=gradients.shape[0])
    
    # reverse the gradients for the 0 labels
    mask = np.copy(labels)
    mask[labels == 0] = -1
    mask = mask.reshape(-1, 1)
    gradients = gradients*mask
    train_num = int(len(gradients)*0.8)
    train_gradients, train_labels = gradients[:train_num], labels[:train_num]
    test_gradients, test_labels = gradients[train_num:], labels[train_num:]

    # train a logistic regression model
    clf = LogisticRegression(random_state=0, penalty='l2', C=1e-4, solver='liblinear') # 
    clf.fit(train_gradients, train_labels)
    print(clf.score(test_gradients, test_labels))

    ## %%
    # projection_matrix = np.load(f"./gradients/{args.dataset_key}_{args.model_key}_{args.preset_key}_{args.project_dim}/projection_matrix_{args.run}.npy")
    proj_coef = clf.coef_.copy().flatten().reshape(-1, 1)
    coef = projection_matrix @ proj_coef.flatten()
    print("L2 norm", np.linalg.norm(coef))
    coef = coef*scale / np.linalg.norm(coef)
    print("L2 norm", np.linalg.norm(coef))

    new_state_dict = generate_state_dict(lm.model, state_dict, coef, device)
    pretrain_state_dict = state_dict
    finetuned_state_dict = new_state_dict

    lm.model.load_state_dict(pretrain_state_dict)
    lm.model.load_state_dict(finetuned_state_dict, strict=False)
    lm.model.eval()

    outputs = []
    for batch_idx, batch in enumerate(data_loader):
        batch = {k: v.to(lm.device) for k, v in batch.items()}
        batch_output = lm.validation_step(batch, batch_idx)
        outputs.append(batch_output)

    summary = evaluate(outputs, lm, tokenizer)
    print(summary)
    return summary
    

def initialize_model(args):
    model_key = args.model_key
    if "flan" in model_key:
        hf_key = "google/{}".format(model_key.replace("_", "-"))
        model = AutoModelForSeq2SeqLM.from_pretrained(hf_key)
        tokenizer = AutoTokenizer.from_pretrained(hf_key, model_max_length=512)
        model_type = "encoder_decoder"
        append_eos = False  # t5 tokenizers already append eos
    elif "t5" in model_key:
        hf_key = model_key.replace("_", "-")
        model = T5ForConditionalGeneration.from_pretrained(hf_key)
        tokenizer = T5TokenizerFast.from_pretrained(hf_key, model_max_length=512)
        model_type = "encoder_decoder"
        append_eos = False
    elif "gpt" in model_key or "Llama" in model_key \
        or "bloomz" in model_key or "gemma" in model_key or "Mistral" in model_key:
        hf_key = args.model_key.replace("_", "-")
        tokenizer = AutoTokenizer.from_pretrained(hf_key)
        if args.use_qlora:
            quantization_config = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_compute_dtype=torch.bfloat16,
                bnb_4bit_use_double_quant=True,
                bnb_4bit_quant_type='nf4'
                )
            model = AutoModelForCausalLM.from_pretrained(hf_key, quantization_config=quantization_config, torch_dtype=torch.bfloat16, device_map={"": args.devices[0]}) #
        else:
            model = AutoModelForCausalLM.from_pretrained(hf_key)
        model_type = "decoder"
        append_eos = True
    else:
        raise NotImplementedError(model_key)
    
    if args.train_lora:
        if args.model_key == "gpt2": # for gpt2, we generally use full model
            config = LoraConfig(
                r=args.lora_rank,
                lora_alpha=args.lora_alpha,
                target_modules=["c_attn", "c_proj", "c_fc"],
                lora_dropout=0.1,
                bias="lora_only",
                modules_to_save=[],
            )
        elif args.model_key == "EleutherAI/gpt-neox-20b":
            config = LoraConfig(
                r=args.lora_rank,
                lora_alpha=args.lora_alpha,
                target_modules=["query_key_value"],
                lora_dropout=0.1,
                bias="lora_only",
                modules_to_save=[],
            )
        elif "flan" in args.model_key:
            config = LoraConfig(
                r=args.lora_rank,
                lora_alpha=args.lora_alpha,
                target_modules=["q", "k", "v"],
                lora_dropout=0.1,
                bias="lora_only",
                modules_to_save=[],
            )
        else:
            config = LoraConfig(
                r=args.lora_rank,
                lora_alpha=args.lora_alpha,
                target_modules=["q_proj", "k_proj", "v_proj"],
                lora_dropout=0.1,
                bias="lora_only",
                modules_to_save=[],
            )
        model = get_peft_model(model, config)
        model.print_trainable_parameters()
    return model, tokenizer, hf_key, model_type, append_eos

def main(args):
    args.enable_checkpointing = not args.disable_checkpointing
    print("arguments".upper().center(80, "-"))
    print(args)
    print("-" * 80)

    dataset_key = args.dataset_key
    model_key = args.model_key
    train_key = args.train_key

    model, tokenizer, hf_key, model_type, append_eos = initialize_model(args)

    if "ft_cot" in args.preset_key:
        completion_key = "ft_cot"
    elif args.preset_key == "ft":
        completion_key = "ft"
    elif args.preset_key == "fs_cot":
        raise NotImplementedError("We don't train models on fs_cot")
    else:
        raise NotImplementedError(args.preset_key)

    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    batch_size = args.batch_size
    if args.inference_batch_size is None:
        inference_batch_size = batch_size
    else:
        inference_batch_size = args.inference_batch_size
    data_module = DataModule(dataset_key, args.preset_key, tokenizer, model_type, batch_size=batch_size,
                                inference_batch_size=inference_batch_size, num_workers=8, append_eos=append_eos)


    data_module.setup("fit")
    train_loader = DataLoader(
                data_module.train_dataset,
                batch_size=data_module.batch_size,
                num_workers=data_module.num_workers,
                shuffle=False)
    test_loader = DataLoader(
                data_module.test_dataset,
                batch_size=data_module.batch_size,
                num_workers=data_module.num_workers,
                shuffle=False)

    cm = CompletionMetadata(model_key, completion_key, dataset_key, prediction_template=data_module.prediction_template)
    lm = Model(model, tokenizer, model_type, completion_metadata=cm, truncate_early=False)
    load_model_dir = args.load_model_dir

    # if load_model_dir is not None:
    #     load_model_dir = os.path.join("external_lightning_logs", load_model_dir)
    #     lm = Model.load_from_checkpoint(load_model_dir + ".ckpt", model=model, tokenizer=tokenizer, model_type=model_type, completion_metadata=cm, truncate_early=False)
    if args.load_model_dir is not None:
        load_model_dir = f"./exported_model/{args.load_model_dir}.pt"
        if os.path.exists(load_model_dir):
            state_dict = torch.load(load_model_dir, map_location=lm.model.device)
            model.load_state_dict(state_dict, strict=False)
            print("Loaded model from checkpoint from ", load_model_dir)

    device = torch.device(f"cuda:{args.devices[0]}")
    lm.completion_metadata = cm
    lm.to(device)

    save_name = f"{args.dataset_key}_{args.model_key}_{args.preset_key}_run_{args.run}_scale_{args.scale}_project_{args.project_dim}" + \
                "_subset_size_{}".format(args.subset_size) + \
                "_clusters_{}".format(args.num_clusters) if args.load_clusters else "" 
    file_dir = os.path.join("./results/", save_name).replace("/", "_")
    if not os.path.exists(file_dir):
        os.mkdir(file_dir)

    state_dict = {key: val.clone() for key, val in lm.model.state_dict().items()}
    pretrain_norm = compute_norm(state_dict, use_lora=args.train_lora)
    print("Norm of the original model", pretrain_norm)
    scale = pretrain_norm * args.scale

    gradient_dim = 0
    remove_keys = ["shared", "lm_head", "wte", "wpe", "ln", "layer_norm", "embed_tokens", "norm"]
    for name, param in model.named_parameters():
        if param.requires_grad:
            if any([rkey in name for rkey in remove_keys]):
                continue
            gradient_dim += param.numel()
    print("Gradient dimension: ", gradient_dim)

    np.random.seed(args.run)
    project_dim = args.project_dim
    project_matrix = (2 * np.random.randint(2, size=(gradient_dim, project_dim)) - 1).astype(float)
    project_matrix *= 1 / np.sqrt(project_dim)

    start = time.time()
    if args.load_sample_task_dir is not None:
        sampled_task_dir = os.path.join("./sampled_indices", "{}.txt".format(args.load_sample_task_dir))

        count = 0
        with open(sampled_task_dir, "r") as f:
            for line in f.readlines():
                if args.load_clusters:
                    subset_idxes = [int(idx) for idx in line.strip().split()]
                    data_idxes = []
                    for idx in subset_idxes:
                        tmp_idxes = np.load(f"./gradients/{args.dataset_key}_{args.model_key}_{args.preset_key}_{args.project_dim}/clusters_{args.num_clusters}/cluster_{idx}.npy") 
                        data_idxes.append(tmp_idxes)
                    data_idxes = np.concatenate(data_idxes)
                    data_idxes.sort()
                    subset_idxes.sort()
                else:
                    subset_idxes = [int(idx) for idx in line.strip().split()]
                    data_idxes = subset_idxes
                    subset_idxes.sort()

                summary = evaluate_subset(args, lm, tokenizer, test_loader, data_idxes, state_dict, device, project_matrix, scale)
                if summary is None:
                    continue

                # save indexes 
                result_datapoint = {
                    "Data indices": " ".join([str(idx) for idx in subset_idxes])
                ,
                }
                for key, val in summary.items():
                    result_datapoint[key] = val
                file_name = os.path.join(file_dir, "results.csv")
                add_result_to_csv(result_datapoint, file_name)
                count += 1
                if count >= args.number_of_subsets:
                    break
    else:
        sampled_task_dir = os.path.join("./sampled_indices", "{}.txt".format(save_name))
        if not os.path.exists(sampled_task_dir):
            f = open(sampled_task_dir, "w")
            f.close()

        for _ in range(args.number_of_subsets):            
            if args.load_clusters:
                subset_idxes = np.random.choice(args.num_clusters, int(args.subset_size*args.num_clusters), replace=False)
                data_idxes = []
                for idx in subset_idxes:
                    tmp_idxes = np.load(f"./gradients/{args.dataset_key}_{args.model_key}_{args.preset_key}_{args.project_dim}/clusters_{args.num_clusters}/cluster_{idx}.npy") 
                    data_idxes.append(tmp_idxes)
                data_idxes = np.concatenate(data_idxes)
                data_idxes.sort()
                subset_idxes.sort()
            else: 
                dataset_len = len(data_module.train_dataset) 
                subset_idxes = np.random.choice(dataset_len, int(args.subset_size*dataset_len), replace=False)
                data_idxes = subset_idxes
                subset_idxes.sort()

            summary = evaluate_subset(args, lm, tokenizer, test_loader, data_idxes, state_dict, device, project_matrix, scale)
            if summary is None:
                    continue
            
            # save indexes 
            result_datapoint = {
                "Data indices": " ".join([str(idx) for idx in subset_idxes])
            }
            for key, val in summary.items():
                result_datapoint[key] = val
            file_name = os.path.join(file_dir, "results.csv")
            add_result_to_csv(result_datapoint, file_name)

            with open(sampled_task_dir, "a") as f:
                f.write(" ".join([str(idx) for idx in subset_idxes]) + "\n")
        end = time.time()
        print("Time taken", end-start)

        # Time taken 71.87238311767578

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset_key", type=str, default="multiarith")
    parser.add_argument("--model_key", type=str, default="flan_t5_base")
    parser.add_argument("--train_key", type=str, default="ft_cot")
    parser.add_argument("--batch_size", type=int, default=32)
    parser.add_argument("--preset_key", type=str, default="ft_cot_t70_64aug")
    parser.add_argument("--inference_batch_size", type=int, default=None)
    parser.add_argument("--devices", type=int, nargs="+", default=[0])
    parser.add_argument("--accumulate", type=int, default=1)
    parser.add_argument("--strategy", type=str, default=None)
    parser.add_argument("--precision", type=int, default=32)
    parser.add_argument("--lr", type=float, default=3e-4)
    parser.add_argument("--disable_checkpointing", action="store_true")

    parser.add_argument("--train_lora", action="store_true")
    parser.add_argument("--lora_rank", type=int, default=4)
    parser.add_argument("--lora_alpha", type=int, default=32)
    parser.add_argument("--use_qlora", action="store_true")

    parser.add_argument("--load_model_dir", type=str, default="flan_t5_base_multiarith_ft_cot_lora_r_4/lightning_logs/version_0/checkpoints/epoch=19-step=51400")
    parser.add_argument("--project_dim", type=int, default=200)
    parser.add_argument("--run", type=int, default=0)

    parser.add_argument("--number_of_subsets", type=int, default=100000)
    parser.add_argument("--subset_size", type=float, default=0.5)
    parser.add_argument("--load_sample_task_dir", type=str, default=None)

    parser.add_argument("--load_clusters", action="store_true")
    parser.add_argument("--num_clusters", type=int, default=200)
    parser.add_argument("--scale", type=float, default=0.05)
    
    args = parser.parse_args()
    main(args)