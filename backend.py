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


# ---------- Extraction Logic for aerospace PDFs ----------
def extract_indent_data(pdf_path):
    rows = []
    upload_time = datetime.now().strftime("%d-%m-%Y %H:%M:%S")

    source_file = os.path.basename(pdf_path)
    file_base = os.path.splitext(source_file)[0]

    last_project_no = None
    batch = db.batch()

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if not text:
                continue

            lines = text.split("\n")
            project_no = item_code = None
            qty = uom = planned_order = planned_start_date = None

            category = None
            if "BOI Item code" in text or "BOI for" in text:
                category = "BOI"
            elif "RM Item code" in text or "RM for" in text:
                category = "RM"

            for line in lines:
                upper = line.upper()

                # -------- PROJECT NO (any format) --------
                if "PROJECT" in upper and "NO" in upper:
                    m = re.search(r"J[A-Z]{2}\d{6}", upper)
                    if m:
                        project_no = m.group().strip()
                        last_project_no = project_no

                if not project_no and last_project_no:
                    project_no = last_project_no

                # -------- ITEM CODE detection (RM / BOI / Fixture / generic) --------
                if "ITEM CODE" in upper:
                    m = re.search(r"[A-Z0-9]{5,}", line)
                    if m:
                        item_code = m.group().strip()

                # -------- PLANNED ORDER detection --------
                if "PLANNED ORDER" in upper:
                    m = re.search(r"(\d+)", line)
                    if m:
                        planned_order = m.group(1)

                # -------- PLANNED START DATE --------
                if "PLANNED START DATE" in upper:
                    m = re.search(r"\d{2}-\d{2}-\d{4}", line)
                    if m:
                        planned_start_date = m.group()

                # -------- QUANTITY detection (supports weight/ech/nos etc) --------
                if "TOTAL" in upper and ("QUANTITY" in upper or "WEIGHT" in upper or "ORDER" in upper):
                    m = re.search(r"([\d,]+(\.\d+)?)[\s]*([A-Za-z%/]+)", line)
                    if m:
                        qty = m.group(1).replace(",", "")
                        uom = m.group(3).strip()

            # -------- If item row completed → push to firestore --------
            if item_code:
                try:
                    qty_val = float(qty) if qty else None
                except:
                    qty_val = qty

                row_id = str(uuid.uuid4())
                row = {
                    "ID": row_id,
                    "PROJECT_NO": project_no,
                    "ITEM_CODE": item_code,
                    "ITEM_DESCRIPTION": None,  # placeholder
                    "REQUIRED_QTY": qty_val,
                    "UOM": uom,
                    "PLANNED_ORDER": planned_order,
                    "PLANNED_START_DATE": planned_start_date,
                    "DATE_OF_UPLOAD": upload_time,
                    "SOURCE_FILE": source_file,
                    "ReferenceB": file_base,
                }

                row["UNIQUE_CODE"] = f"{file_base}{project_no}{item_code}"
                rows.append(row)

                doc_ref = indent_collection.document(row_id)
                batch.set(doc_ref, row)

    # -------- Commit batch write to Firestore --------
    if rows:
        batch.commit()

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
