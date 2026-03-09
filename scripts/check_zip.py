import zipfile
import re
import os

doc_path = r"c:\Users\AbhidyuMahajan\OneDrive - ProcDNA Analytics Pvt. Ltd\Desktop\Projects\Gilead_Demo\Input\GILEAD Field Inquiry Script v1.0.docx"

print(f"File exists: {os.path.exists(doc_path)}")
try:
    with zipfile.ZipFile(doc_path) as z:
        print("It is a valid ZIP file.")
        xml_content = z.read("word/document.xml").decode("utf-8")
        
        # simple namespace extraction
        # text is inside <w:t> tags. paragraphs are inside <w:p> tags.
        paragraphs = re.findall(r'<w:p[^>]*>.*?</w:p>', xml_content)
        extracted = []
        for p in paragraphs:
            texts = re.findall(r'<w:t[^>]*>(.*?)</w:t>', p)
            if texts:
                extracted.append("".join(texts))
        
        print(f"Extracted {len(extracted)} paragraphs.")
        for i, text in enumerate(extracted[:10]):
            print(f"{i}: {text[:50]}")
            
except Exception as e:
    print(f"Error: {e}")
