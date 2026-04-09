""" 
Copyright (c) 2025 Bytedance Ltd. and/or its affiliates
SPDX-License-Identifier: MIT
"""

import argparse
import glob
import os
from datetime import datetime

import torch
from PIL import Image
from transformers import AutoProcessor, Qwen2_5_VLForConditionalGeneration
from qwen_vl_utils import process_vision_info

from utils.utils import *


DEFAULT_LANGUAGE_CONSTRAINT_TEXT = (
    "Language constraint: Keep the original language from the image. "
    "Do not translate or add Chinese/Japanese text unless it clearly exists in the source. "
    "Preserve symbols, numbers, and formatting as faithfully as possible."
)


def _sanitize_name(text):
    """Keep only alphanumeric characters for stable output folder names."""
    sanitized = "".join(ch for ch in text if ch.isalnum())
    return sanitized or "input"


def build_run_save_dir(base_save_dir, input_path, run_tag=None):
    """Build a run-specific output directory: <name>_<yymmdd>[_<tag>]."""
    input_name = os.path.splitext(os.path.basename(input_path.rstrip("/\\")))[0]
    input_name = _sanitize_name(input_name)
    date_part = datetime.now().strftime("%y%m%d")

    dir_name = f"{input_name}_{date_part}"
    if run_tag:
        dir_name = f"{dir_name}_{_sanitize_name(run_tag)}"

    return os.path.join(base_save_dir, dir_name)


def normalize_source_languages(raw_values):
    """Normalize --source_languages input into a clean list."""
    if not raw_values:
        return []

    normalized = []
    for value in raw_values:
        for token in str(value).split(","):
            token = token.strip()
            if token:
                normalized.append(token)
    return normalized


def apply_language_constraint(base_prompt, use_constraint=False, constraint_text="", source_languages=None):
    """Append language-constraint instructions only when enabled."""
    if not use_constraint:
        return base_prompt

    text = (constraint_text or "").strip()
    language_hint = ""
    if source_languages:
        language_hint = (
            f"The source languages in this image are: {', '.join(source_languages)}. "
            "Keep these languages as-is as much as possible."
        )

    parts = [part for part in [text, language_hint] if part]
    if not parts:
        return base_prompt

    return f"{base_prompt}\n\n{' '.join(parts)}"


class DOLPHIN:
    def __init__(self, model_id_or_path):
        """Initialize the Hugging Face model
        
        Args:
            model_id_or_path: Path to local model or Hugging Face model ID
        """
        # Set device and precision first to avoid peak memory at load time
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        load_dtype = torch.bfloat16 if self.device == "cuda" else torch.float32

        # Load processor/model from local path or Hugging Face hub
        self.processor = AutoProcessor.from_pretrained(model_id_or_path)
        self.model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
            model_id_or_path,
            torch_dtype=load_dtype,
            low_cpu_mem_usage=True,
        )
        self.model.eval()
        self.model.to(self.device)
        
        # set tokenizer
        self.tokenizer = self.processor.tokenizer
        self.tokenizer.padding_side = "left"

    def chat(self, prompt, image):
        # Check if we're dealing with a batch
        is_batch = isinstance(image, list)
        
        if not is_batch:
            # Single image, wrap it in a list for consistent processing
            images = [image]
            prompts = [prompt]
        else:
            # Batch of images
            images = image
            prompts = prompt if isinstance(prompt, list) else [prompt] * len(images)
        
        assert len(images) == len(prompts)
        
        # preprocess all images
        processed_images = [resize_img(img) for img in images]
        # generate all messages
        all_messages = []
        for img, question in zip(processed_images, prompts):
            messages = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "image": img,
                        },
                        {"type": "text", "text": question}
                    ],
                }
            ]
            all_messages.append(messages)

        # prepare all texts
        texts = [
            self.processor.apply_chat_template(
                msgs, tokenize=False, add_generation_prompt=True
            )
            for msgs in all_messages
        ]

        # collect all image inputs
        all_image_inputs = []
        all_video_inputs = None
        for msgs in all_messages:
            image_inputs, video_inputs = process_vision_info(msgs)
            all_image_inputs.extend(image_inputs)

        # prepare model inputs
        inputs = self.processor(
            text=texts,
            images=all_image_inputs if all_image_inputs else None,
            videos=all_video_inputs if all_video_inputs else None,
            padding=True,
            return_tensors="pt",
        )
        inputs = inputs.to(self.model.device)

        # inference
        generated_ids = self.model.generate(
            **inputs,
            max_new_tokens=4096,
            do_sample=False,
            temperature=None,
            # repetition_penalty=1.05
        )
        generated_ids_trimmed = [
            out_ids[len(in_ids):] 
            for in_ids, out_ids in zip(inputs.input_ids, generated_ids)
        ]
        
        results = self.processor.batch_decode(
            generated_ids_trimmed, 
            skip_special_tokens=True, 
            clean_up_tokenization_spaces=False
        )

        # Return a single result for single image input
        if not is_batch:
            return results[0]
        return results


def process_document(
    document_path,
    model,
    save_dir,
    max_batch_size=None,
    pdf_target_size=896,
    use_language_constraint_prompt=False,
    language_constraint_text="",
    source_languages=None,
):
    """Parse documents with two stages - Handles both images and PDFs"""
    file_ext = os.path.splitext(document_path)[1].lower()
    
    if file_ext == '.pdf':
        # Convert PDF to images
        images = convert_pdf_to_images(document_path, target_size=pdf_target_size)
        if not images:
            raise Exception(f"Failed to convert PDF {document_path} to images")
        
        all_results = []
        
        # Process each page
        for page_idx, pil_image in enumerate(images):
            print(f"Processing page {page_idx + 1}/{len(images)}")
            
            # Generate output name for this page
            base_name = os.path.splitext(os.path.basename(document_path))[0]
            page_name = f"{base_name}_page_{page_idx + 1:03d}"
            
            # Process this page (don't save individual page results)
            json_path, recognition_results = process_single_image(
                pil_image,
                model,
                save_dir,
                page_name,
                max_batch_size,
                save_individual=False,
                use_language_constraint_prompt=use_language_constraint_prompt,
                language_constraint_text=language_constraint_text,
                source_languages=source_languages,
            )
            
            # Add page information to results
            page_results = {
                "page_number": page_idx + 1,
                "elements": recognition_results
            }
            all_results.append(page_results)
        
        # Save combined results for multi-page PDF
        combined_json_path = save_combined_pdf_results(all_results, document_path, save_dir)
        
        return combined_json_path, all_results
    
    else:
        # Process regular image file
        pil_image = Image.open(document_path).convert("RGB")
        base_name = os.path.splitext(os.path.basename(document_path))[0]
        return process_single_image(
            pil_image,
            model,
            save_dir,
            base_name,
            max_batch_size,
            use_language_constraint_prompt=use_language_constraint_prompt,
            language_constraint_text=language_constraint_text,
            source_languages=source_languages,
        )


def process_single_image(
    image,
    model,
    save_dir,
    image_name,
    max_batch_size=None,
    save_individual=True,
    use_language_constraint_prompt=False,
    language_constraint_text="",
    source_languages=None,
):
    """Process a single image (either from file or converted from PDF page)
    
    Args:
        image: PIL Image object
        model: DOLPHIN model instance
        save_dir: Directory to save results
        image_name: Name for the output file
        max_batch_size: Maximum batch size for processing
        save_individual: Whether to save individual results (False for PDF pages)
        
    Returns:
        Tuple of (json_path, recognition_results)
    """
    # Stage 1: Page-level layout and reading order parsing
    layout_output = model.chat("Parse the reading order of this document.", image)
    # print(layout_output)

    # Stage 2: Element-level content parsing
    recognition_results = process_elements(
        layout_output,
        image,
        model,
        max_batch_size,
        save_dir,
        image_name,
        use_language_constraint_prompt=use_language_constraint_prompt,
        language_constraint_text=language_constraint_text,
        source_languages=source_languages,
    )

    # Save outputs only if requested (skip for PDF pages)
    json_path = None
    if save_individual:
        # Create a dummy image path for save_outputs function
        json_path = save_outputs(recognition_results, image, image_name, save_dir)

    return json_path, recognition_results


def process_elements(
    layout_results,
    image,
    model,
    max_batch_size,
    save_dir=None,
    image_name=None,
    use_language_constraint_prompt=False,
    language_constraint_text="",
    source_languages=None,
):
    """Parse all document elements with parallel decoding"""
    layout_results_list = parse_layout_string(layout_results)
    if not layout_results_list or not (layout_results.startswith("[") and layout_results.endswith("]")):
        layout_results_list = [([0, 0, *image.size], 'distorted_page', [])]
    # Check for bbox overlap - if too many overlaps, treat as distorted page
    elif len(layout_results_list) > 1 and check_bbox_overlap(layout_results_list, image):
        print("Falling back to distorted_page mode due to high bbox overlap")
        layout_results_list = [([0, 0, *image.size], 'distorted_page', [])]
        
    tab_elements = []      
    equ_elements = []     
    code_elements = []    
    text_elements = []     
    figure_results = []    
    reading_order = 0

    # Collect elements and group
    for bbox, label, tags in layout_results_list:
        try:
            if label == "distorted_page":
                x1, y1, x2, y2 = 0, 0, *image.size
                pil_crop = image
            else:
                # get coordinates in the original image
                x1, y1, x2, y2 = process_coordinates(bbox, image)
                # crop the image
                pil_crop = image.crop((x1, y1, x2, y2))

            if pil_crop.size[0] > 3 and pil_crop.size[1] > 3:
                if label == "fig":
                    figure_filename = save_figure_to_local(pil_crop, save_dir, image_name, reading_order)
                    figure_results.append({
                        "label": label,
                        "text": f"![Figure](figures/{figure_filename})",
                        "figure_path": f"figures/{figure_filename}",
                        "bbox": [x1, y1, x2, y2],
                        "reading_order": reading_order,
                        "tags": tags,
                    })
                else:
                    # Prepare element information
                    element_info = {
                        "crop": pil_crop,
                        "label": label,
                        "bbox": [x1, y1, x2, y2],
                        "reading_order": reading_order,
                        "tags": tags,
                    }
                    
                    if label == "tab":
                        tab_elements.append(element_info)
                    elif label == "equ":
                        equ_elements.append(element_info)
                    elif label == "code":
                        code_elements.append(element_info)
                    else:
                        text_elements.append(element_info)

            reading_order += 1

        except Exception as e:
            print(f"Error processing bbox with label {label}: {str(e)}")
            continue

    recognition_results = figure_results.copy()
    table_prompt = apply_language_constraint(
        "Parse the table in the image.",
        use_language_constraint_prompt,
        language_constraint_text,
        source_languages,
    )
    formula_prompt = apply_language_constraint(
        "Read formula in the image.",
        use_language_constraint_prompt,
        language_constraint_text,
        source_languages,
    )
    code_prompt = apply_language_constraint(
        "Read code in the image.",
        use_language_constraint_prompt,
        language_constraint_text,
        source_languages,
    )
    text_prompt = apply_language_constraint(
        "Read text in the image.",
        use_language_constraint_prompt,
        language_constraint_text,
        source_languages,
    )
    
    if tab_elements:
        results = process_element_batch(tab_elements, model, table_prompt, max_batch_size)
        recognition_results.extend(results)
    
    if equ_elements:
        results = process_element_batch(equ_elements, model, formula_prompt, max_batch_size)
        recognition_results.extend(results)
    
    if code_elements:
        results = process_element_batch(code_elements, model, code_prompt, max_batch_size)
        recognition_results.extend(results)
    
    if text_elements:
        results = process_element_batch(text_elements, model, text_prompt, max_batch_size)
        recognition_results.extend(results)

    recognition_results.sort(key=lambda x: x.get("reading_order", 0))

    return recognition_results


def process_element_batch(elements, model, prompt, max_batch_size=None):
    """Process elements of the same type in batches"""
    results = []
    
    # Determine batch size
    batch_size = len(elements)
    if max_batch_size is not None and max_batch_size > 0:
        batch_size = min(batch_size, max_batch_size)
    
    # Process in batches
    for i in range(0, len(elements), batch_size):
        batch_elements = elements[i:i+batch_size]
        crops_list = [elem["crop"] for elem in batch_elements]
        
        # Use the same prompt for all elements in the batch
        prompts_list = [prompt] * len(crops_list)
        
        # Batch inference
        batch_results = model.chat(prompts_list, crops_list)
        
        # Add results
        for j, result in enumerate(batch_results):
            elem = batch_elements[j]
            results.append({
                "label": elem["label"],
                "bbox": elem["bbox"],
                "text": result.strip(),
                "reading_order": elem["reading_order"],
                "tags": elem["tags"],
            })
    
    return results


def main():
    parser = argparse.ArgumentParser(description="Document parsing based on DOLPHIN")
    parser.add_argument("--model_path", default="./hf_model", help="Path to Hugging Face model")
    parser.add_argument("--input_path", type=str, default="./demo", help="Path to input image/PDF or directory of files")
    parser.add_argument(
        "--save_dir",
        type=str,
        default=None,
        help="Directory to save parsing results (default: same as input directory)",
    )
    parser.add_argument(
        "--max_batch_size",
        type=int,
        default=4,
        help="Maximum number of document elements to parse in a single batch (default: 4)",
    )
    parser.add_argument(
        "--pdf_target_size",
        type=int,
        default=896,
        help="Target size for PDF page rendering before parsing (default: 896)",
    )
    parser.add_argument(
        "--run_tag",
        type=str,
        default="",
        help="Optional run tag appended to auto-created output subdirectory name",
    )
    parser.add_argument(
        "--use_language_constraint_prompt",
        action="store_true",
        help="Use prompts with additional language-preservation constraints",
    )
    parser.add_argument(
        "--language_constraint_text",
        type=str,
        default=DEFAULT_LANGUAGE_CONSTRAINT_TEXT,
        help="Constraint text appended when --use_language_constraint_prompt is enabled",
    )
    parser.add_argument(
        "--source_languages",
        nargs="*",
        default=None,
        help="Optional source language list (e.g., --source_languages Korean English)",
    )
    args = parser.parse_args()
    source_languages = normalize_source_languages(args.source_languages)
    use_language_constraint_prompt = args.use_language_constraint_prompt or bool(source_languages)

    # Load Model
    model = DOLPHIN(args.model_path)

    # Collect Document Files (images and PDFs)
    if os.path.isdir(args.input_path):
        # Support both image and PDF files
        file_extensions = [".jpg", ".jpeg", ".png", ".JPG", ".JPEG", ".PNG", ".pdf", ".PDF"]
        
        document_files = []
        for ext in file_extensions:
            document_files.extend(glob.glob(os.path.join(args.input_path, f"*{ext}")))
        document_files = sorted(document_files)
    else:
        if not os.path.exists(args.input_path):
            raise FileNotFoundError(f"Input path {args.input_path} does not exist")
        
        # Check if it's a supported file type
        file_ext = os.path.splitext(args.input_path)[1].lower()
        supported_exts = ['.jpg', '.jpeg', '.png', '.pdf']
        
        if file_ext not in supported_exts:
            raise ValueError(f"Unsupported file type: {file_ext}. Supported types: {supported_exts}")
        
        document_files = [args.input_path]

    save_dir = args.save_dir or (
        args.input_path if os.path.isdir(args.input_path) else os.path.dirname(args.input_path)
    )
    if args.save_dir:
        save_dir = build_run_save_dir(save_dir, args.input_path, args.run_tag)

    print(f"Output directory: {save_dir}")
    setup_output_dirs(save_dir)

    total_samples = len(document_files)
    print(f"\nTotal files to process: {total_samples}")

    # Process All Document Files
    for file_path in document_files:
        print(f"\nProcessing {file_path}")
        try:
            json_path, recognition_results = process_document(
                document_path=file_path,
                model=model,
                save_dir=save_dir,
                max_batch_size=args.max_batch_size,
                pdf_target_size=args.pdf_target_size,
                use_language_constraint_prompt=use_language_constraint_prompt,
                language_constraint_text=args.language_constraint_text,
                source_languages=source_languages,
            )

            print(f"Processing completed. Results saved to {save_dir}")

        except Exception as e:
            print(f"Error processing {file_path}: {str(e)}")
            continue


if __name__ == "__main__":
    main()
