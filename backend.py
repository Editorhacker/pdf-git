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

# ---------- Firestore Connection Test ----------
try:
    test_ref = indent_collection.document("connection-test")
    test_ref.set({"status": "ok", "time": datetime.now().isoformat()})
    print("✅ Firestore connection test successful")
except Exception as e:
    print("❌ Firestore connection failed:", e)

print("📂 Connected Firestore project:", cred_dict.get("project_id"))


# ---------- Regex Patterns (unchanged) ----------
row_pattern_rm = re.compile(
    r"(?P<project>\S+)\s*:?\s*RM\s*Item\s*code\s*:\s*(?P<item>\S+)\s*-\s*"
    r"(?P<qty>[\d.]+)\s*(?P<uom>[A-Z]+)\s*"
    r"(?P<order>\d+)\s*(?P<date>\d{2}-\d{2}-\d{4})",
    flags=re.I
)

row_pattern_item = re.compile(
    r"(?P<project>\S+)\s*:?\s*Item\s*code\s*:\s*(?P<item>\S+)\s*-\s*"
    r"(?P<qty>[\d.]+)\s*(?P<uom>[A-Z]+)\s*"
    r"(?P<order>\d+)\s*(?P<date>\d{2}-\d{2}-\d{4})",
    flags=re.I
)

row_pattern_plain = re.compile(
    r"(?P<project>\S+)\s+(?P<item>\S+)\s*-\s*"
    r"(?P<qty>[\d.]+)\s*(?P<uom>[A-Z]+)\s*"
    r"(?P<order>\d+)\s*(?P<date>\d{2}-\d{2}-\d{4})",
    flags=re.I
)

# ---------- Extraction Logic ----------
# ---------- Extraction Logic (Multi-line support) ----------
# ---------- Extraction Logic (Combined Single & Multi-line) ----------
def extract_indent_data(pdf_path):
    rows = []
    upload_time = datetime.now().strftime("%d-%m-%Y %H:%M:%S")

    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            text = page.extract_text()
            if not text:
                continue

            lines = [line.strip() for line in text.split("\n") if line.strip()]

            # Temporary variables for multi-line row
            project_no, item_code, item_desc = None, None, None
            qty, uom, planned_order, planned_start_date = None, None, None, None

            for line_num, line in enumerate(lines, start=1):
                upper_line = line.upper()
                # ---------- Case 1: Full row in one line ----------
                match = row_pattern.search(line)
                if match:
                    project_no = match.group(1)
                    item_code = match.group(2)
                    try:
                        qty_val = float(match.group(3))
                    except:
                        qty_val = match.group(3)
                    uom = match.group(4)
                    planned_order = match.group(5)
                    planned_start_date = match.group(6)

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
                        "SOURCE_FILE": os.path.basename(pdf_path),
                    }
                    rows.append(row)
                    indent_collection.document(row["ID"]).set(row)
                    continue

                # ---------- Case 2: Multi-line key/value ----------
                if "PROJECT NO" in upper_line:
                    project_no = re.sub(r":?\s*Project\s*No\s*[:\-]?\s*", "", line, flags=re.I).strip()

                if "ITEM CODE" in upper_line:
                    item_code = re.sub(r":?\s*BOI\s*Item\s*code\s*[:\-]?\s*", "", line, flags=re.I).strip()

                if "PLAN ITEM" in upper_line and ":" in line:
                    match = re.search(r"([A-Z]+\d+)([0-9A-Z]+)", line.split(":")[-1].strip())
                    if match:
                        project_no = match.group(1)
                        item_code = match.group(2)

                if "PART DESCRIPTION" in upper_line or "PART NUMBER AND DESCRIPTION" in upper_line:
                    item_desc = re.sub(r":?\s*Part.*Description\s*[:\-]?\s*", "", line, flags=re.I).strip()

                if "TOTAL ORDER QUANTITY" in upper_line or "TOTAL QUANTITY" in upper_line:
                    qty_part = line.split(":", 1)[1].strip() if ":" in line else line.strip()
                    parts = qty_part.split()
                    qty = parts[0]
                    uom = parts[1] if len(parts) > 1 else None

                if "PLANNED ORDER" in upper_line and ":" in line:
                    planned_order = line.split(":", 1)[1].strip().split()[0]

                if "PLANNED START DATE" in upper_line:
                    planned_start_date = line.split(":")[-1].strip()

            # ---------- Save multi-line row ----------
            if item_code:
                try:
                    qty_val = float(qty) if qty else None
                except:
                    qty_val = qty

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
                    "SOURCE_FILE": os.path.basename(pdf_path),
                }
                rows.append(row)
                indent_collection.document(row["ID"]).set(row)

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
