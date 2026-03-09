import zipfile
import re
import os
import shutil
import json

doc_path = r"c:\Users\AbhidyuMahajan\OneDrive - ProcDNA Analytics Pvt. Ltd\Desktop\Projects\Gilead_Demo\Input\GILEAD Field Inquiry Script v1.0.docx"
temp_doc_path = r"c:\Users\AbhidyuMahajan\OneDrive - ProcDNA Analytics Pvt. Ltd\Desktop\Projects\Gilead_Demo\Input\temp_script.docx"
json_path = r"c:\Users\AbhidyuMahajan\OneDrive - ProcDNA Analytics Pvt. Ltd\Desktop\Projects\Gilead_Demo\Input\GILEAD_Field_Inquiry_Playbook.json"

shutil.copy2(doc_path, temp_doc_path)

from docx import Document

doc = Document(temp_doc_path)
resolutions = []
current_resolution = []
in_resolution = False

for para in doc.paragraphs:
    text = para.text.strip()
    if text.startswith("Resolution and Response to Rep:"):
        in_resolution = True
        rest = text[len("Resolution and Response to Rep:"):].strip()
        if rest:
            clean_text = rest.replace(" — ", ", ").replace(" - ", ", ").replace("—",", ").replace("- ",", ")
            current_resolution.append(clean_text)
        continue
        
    if in_resolution:
        if text.startswith("Inquiry ID:") or text.startswith("Scenario ID:") or text.startswith("Category:") or text.startswith("Title:") or text.startswith("What Happened:") or text.startswith("System Impact:"):
            if current_resolution:
                resolutions.append("\n\n".join(current_resolution).strip())
                current_resolution = []
            in_resolution = False
            continue
        
        if text:
            clean_text = text.replace(" — ", ", ").replace(" - ", ", ").replace("—",", ").replace("- ",", ")
            current_resolution.append(clean_text)

if current_resolution:
    resolutions.append("\n\n".join(current_resolution).strip())

try:
    os.remove(temp_doc_path)
except:
    pass

with open(json_path, 'r', encoding='utf-8') as f:
    data = json.load(f)

print(f"Found {len(resolutions)} resolutions in the Word document.")

if len(resolutions) == len(data.get("inquiries", [])):
    for i, item in enumerate(data["inquiries"]):
        item["resolution_and_response_to_rep"] = resolutions[i]
        
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print("Successfully updated the JSON with Word document formatting.")
else:
    print(f"Error: Found {len(resolutions)} resolutions, but JSON has {len(data.get('inquiries', []))} inquiries.")
    for i in range(min(5, len(resolutions))):
        print(f"--- Resolution {i+1} ---")
        print(resolutions[i])
