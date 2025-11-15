import pdfplumber
import json
import re
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import uuid
import firebase_admin
from firebase_admin import credentials, firestore
from werkzeug.utils import secure_filename

# ---------- Flask App ----------
app = Flask(__name__)
CORS(app)

OUTPUT_JSON = "indent_data.json"
UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ---------- Firestore Setup ----------
firebase_json = os.getenv("FIREBASE_CREDENTIALS")
if not firebase_json:
    raise Exception("FIREBASE_CREDENTIALS env var not set")

cred_dict = json.loads(firebase_json)
cred = credentials.Certificate(cred_dict)

firebase_admin.initialize_app(cred)
db = firestore.client()
indent_collection = db.collection("Indent_Quantity")

# ---------- Regex patterns ----------
row_pattern = re.compile(
    r"""
    Project\s*No\s*[:\-]?\s*(JLE\d+)\s+          
    Item\s*code\s*[:\-]?\s*([A-Z0-9]+)\s+       
    -\s*(\d+\.?\d*)\s+                           
    (\w+)\s+                                     
    (\d+)\s+                                     
    (\d{2}-\d{2}-\d{4})                          
    """,
    flags=re.I | re.VERBOSE
)

# ---------- Extraction Logic ----------
def extract_indent_data(pdf_path):
    rows = []
    upload_time = datetime.now().strftime("%d-%m-%Y %H:%M:%S")

    source_file = os.path.basename(pdf_path)
    file_base = os.path.splitext(source_file)[0]  # filename without .pdf

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if not text:
                continue

            lines = text.split("\n")
            project_no = item_code = item_desc = None
            qty = uom = planned_order = planned_start_date = None

            for line in lines:
                upper_line = line.upper()
                match = row_pattern.search(line)

                # -------- Single-line extraction --------
                if match:
                    project_no = match.group(1).strip().upper()
                    item_code = match.group(2).strip().upper()
                    qty_val = float(match.group(3)) if match.group(3) else None
                    uom = match.group(4).strip()
                    planned_order = match.group(5).strip()
                    planned_start_date = match.group(6).strip()

                    row = {
                        "ID": str(uuid.uuid4()),
                        "PROJECT_NO": project_no,
                        "ITEM_CODE": item_code,
                        "ITEM_DESCRIPTION": None,
                        "REQUIRED_QTY": qty_val,
                        "UOM": uom,
                        "PLANNED_ORDER": planned_order,
                        "PLANNED_START_DATE": planned_start_date,
                        "DATE_OF_UPLOAD": upload_time,
                        "SOURCE_FILE": source_file,
                    }
                    rows.append(row)
                    continue

                # -------- Multi-line extraction --------
                if "PROJECT NO" in upper_line:
                    m = re.search(r"JLE\d+", line)
                    if m:
                        project_no = m.group().strip().upper()

                if "ITEM CODE" in upper_line:
                    m = re.search(r"([A-Z0-9]+)$", line.strip())
                    if m:
                        item_code = m.group().strip().upper()

                if "PART DESCRIPTION" in upper_line:
                    item_desc = re.sub(r".*:\s*", "", line).strip()

                if "TOTAL ORDER QUANTITY" in upper_line and ":" in line:
                    qty_part = line.split(":", 1)[1].strip().split()
                    qty = qty_part[0]
                    uom = qty_part[1] if len(qty_part) > 1 else None

                if "PLANNED ORDER" in upper_line and ":" in line:
                    planned_order = line.split(":")[1].strip().split()[0]

                if "PLANNED START DATE" in upper_line:
                    planned_start_date = line.split(":")[-1].strip()

            # Save multi-line
            if item_code:
                qty_val = float(qty) if qty else None

                row = {
                    "ID": str(uuid.uuid4()),
                    "PROJECT_NO": project_no,
                    "ITEM_CODE": item_code,
                    "ITEM_DESCRIPTION": item_desc,
                    "REQUIRED_QTY": qty_val,
                    "UOM": uom,
                    "PLANNED_ORDER": planned_order,
                    "PLANNED_START_DATE": planned_start_date,
                    "DATE_OF_UPLOAD": upload_time,
                    "SOURCE_FILE": source_file,
                }
                rows.append(row)

    # -------- Append UNIQUE_CODE after all rows extracted --------
    for r in rows:
        r["UNIQUE_CODE"] = f"{file_base}{r['PROJECT_NO']}{r['ITEM_CODE']}"
        indent_collection.document(r["ID"]).set(r)  # store AFTER adding UNIQUE_CODE

    return rows

# ---------- API Endpoints ----------
@app.route("/upload", methods=["POST"])
def upload_files():
    if "files" not in request.files:
        return jsonify({"error": "No files provided"}), 400

    files = request.files.getlist("files")
    all_indent_data = []
    file_summary = {}

    for f in files:
        safe_filename = secure_filename(f.filename)
        save_path = os.path.join(UPLOAD_FOLDER, safe_filename)
        f.save(save_path)

        try:
            indent_data = extract_indent_data(save_path)
            all_indent_data.extend(indent_data)

            file_summary[safe_filename] = {
                "items_extracted": len(indent_data),
                "status": "Success"
            }
        except Exception as e:
            file_summary[safe_filename] = {
                "items_extracted": 0,
                "status": f"Error: {str(e)}"
            }

    output_data = {
        "indent_data": all_indent_data,
        "extraction_timestamp": datetime.now().strftime("%d-%m-%Y %H:%M:%S"),
        "total_items": len(all_indent_data),
        "total_files_processed": len(file_summary),
        "file_summary": file_summary,
        "unique_item_codes": len(set(item["ITEM_CODE"] for item in all_indent_data if "ITEM_CODE" in item))
    }

    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(output_data, f, indent=4, ensure_ascii=False)

    return jsonify(output_data)

@app.route("/download", methods=["GET"])
def download_json():
    if not os.path.exists(OUTPUT_JSON):
        return jsonify({"error": "JSON not found"}), 404

    with open(OUTPUT_JSON, "r", encoding="utf-8") as f:
        data = json.load(f)
    return jsonify(data)

# ---------- Run App ----------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)
