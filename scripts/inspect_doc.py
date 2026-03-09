from docx import Document

doc_path = r"c:\Users\AbhidyuMahajan\OneDrive - ProcDNA Analytics Pvt. Ltd\Desktop\Projects\Gilead_Demo\Input\GILEAD Field Inquiry Script v1.0.docx"
doc = Document(doc_path)

for i, para in enumerate(doc.paragraphs[:50]):
    text = para.text.strip()
    if text:
        print(f"[{i}] {text}")
