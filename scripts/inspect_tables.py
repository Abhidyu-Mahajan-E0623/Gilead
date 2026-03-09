from docx import Document
import json

doc_path = r"c:\Users\AbhidyuMahajan\OneDrive - ProcDNA Analytics Pvt. Ltd\Desktop\Projects\Gilead_Demo\Input\GILEAD Field Inquiry Script v1.0.docx"
doc = Document(doc_path)

for r, row in enumerate(doc.tables[1].rows[:25]):
    for c, cell in enumerate(row.cells):
        text = cell.text.strip()
        if text:
            print(f"R{r}C{c}: {text[:100]}...")
