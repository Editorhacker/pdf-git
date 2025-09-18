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
def extract_indent_data(pdf_path):
    rows = []
    upload_time = datetime.now().strftime("%d-%m-%Y %H:%M:%S")
    batch = db.batch()  # Firestore batch write

    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            text = page.extract_text()
            if not text:
                print(f"⚠️ Page {page_num} has no extractable text")
                continue

            lines = [line.strip() for line in text.split("\n") if line.strip()]
            # Temporary variables for one logical row
            project_no, item_code, item_desc = None, None, None
            qty_val, uom, planned_order, planned_start_date = None, None, None, None

            for line_num, line in enumerate(lines, start=1):
                line_upper = line.upper()
                print(f"Processing Page {page_num} Line {line_num}: {line}")

                # ---------- Multi-line key/value detection ----------
                if "PROJECT NO" in line_upper:
                    project_no = re.sub(r":?\s*Project\s*No\s*[:\-]?\s*", "", line, flags=re.I).strip()
                elif "ITEM CODE" in line_upper:
                    item_code = re.sub(r":?\s*BOI\s*Item\s*code\s*[:\-]?\s*", "", line, flags=re.I).strip()
                elif "PART NUMBER AND DESCRIPTION" in line_upper:
                    item_desc = re.sub(r":?\s*Part Number and Description\s*[:\-]?\s*", "", line, flags=re.I).strip()
                elif "TOTAL QUANTITY" in line_upper:
                    qty_part = re.sub(r":?\s*Total Quantity\s*[:\-]?\s*", "", line, flags=re.I).strip()
                    parts = qty_part.split()
                    try:
                        qty_val = float(parts[0])
                    except:
                        qty_val = None
                    uom = parts[1] if len(parts) > 1 else None
                elif "PLANNED ORDER" in line_upper:
                    planned_order = re.sub(r":?\s*Planned Order\s*[:\-]?\s*", "", line, flags=re.I).strip()
                elif "PLANNED START DATE" in line_upper:
                    planned_start_date = re.sub(r":?\s*Planned Start Date\s*[:\-]?\s*", "", line, flags=re.I).strip()

                # ---------- Detect end of row block ----------
                if project_no and item_code and qty_val is not None:
                    # Build row dictionary
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
                    doc_ref = indent_collection.document(row["ID"])
                    batch.set(doc_ref, row)
                    print(f"✅ Queued row {row['ID']} for Firestore")

                    # Reset temp variables for next row
                    project_no, item_code, item_desc = None, None, None
                    qty_val, uom, planned_order, planned_start_date = None, None, None, None

    # Commit all queued writes
    if rows:
        try:
            batch.commit()
            print(f"✅ Batch commit successful, {len(rows)} rows written")
        except Exception as e:
            print(f"❌ Batch commit failed: {e}")
    else:
        print("⚠️ No valid rows found to commit")

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
