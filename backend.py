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


# ---------- Extraction Logic ----------
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

            # PAGE VARIABLES
            project_no = None
            item_code = None
            qty = None
            uom = None
            planned_order = None
            planned_start_date = None

            # ---------- Detect RM / BOI Category ----------
            category = None
            if "BOI Item code" in text or "BOI for" in text:
                category = "BOI"
            elif "RM Item code" in text or "RM for" in text:
                category = "RM"

            # ---------- Extract Material Description ----------
            material_spec = None
            material_size = None

            spec_match = re.search(r"Material Spec/ Std\s*-\s*(.*)", text)
            if spec_match:
                material_spec = spec_match.group(1).strip()

            size_match = re.search(r"Material Size and Qty\s*-\s*(.*)", text)
            if size_match:
                material_size = size_match.group(1).strip()

            item_description = (
                f"{material_spec} {material_size}"
                if material_spec and material_size
                else None
            )

            # ---------- Extract Material Type + RM Form ----------
            material_type = None
            rm_form = None

            type_match = re.search(r"Material Type\s*-\s*(.*)", text)
            if type_match:
                material_type = type_match.group(1).strip()

            form_match = re.search(r"RM Form\s*-\s*(.*)", text)
            if form_match:
                rm_form = form_match.group(1).strip()

            final_type = None
            if material_type and rm_form:
                final_type = f"{rm_form}{material_type}".replace(" ", "")

            # ---------- Extract Fixture Code (ERxxxx_xxxxxx) ----------
            fixture_code = None

            plan_fixture_match = re.search(
                r"Plan Item\s*:\s*[A-Z0-9]+\s+(ER[0-9]+_[A-Z0-9]+)", text
            )
            if plan_fixture_match:
                fixture_code = plan_fixture_match.group(1).strip()

            # ---------- Extract Plan Item Description ----------
            plan_item_description = None

            plan_desc_match = re.search(
                r"Plan Item\s*:\s*[A-Z0-9]+\s+(.*)", text
            )
            if plan_desc_match:
                plan_item_description = plan_desc_match.group(1).strip()

            # ---------- Line-by-Line Extraction ----------
            for line in lines:
                upper = line.upper()

                # ---- PROJECT NO ----
                if "PROJECT" in upper and "NO" in upper:
                    m = re.search(r"J[A-Z]{2}\d{6}", upper)
                    if m:
                        project_no = m.group().strip()
                        last_project_no = project_no

                if not project_no and last_project_no:
                    project_no = last_project_no

                # ---- ITEM CODE ----
                if "ITEM CODE" in upper:
                    m = re.search(r"[A-Z0-9]{5,}", line)
                    if m:
                        item_code = m.group().strip()

                # ---- PLANNED ORDER ----
                if "PLANNED ORDER" in upper:
                    m = re.search(r"(\d+)", line)
                    if m:
                        planned_order = m.group(1)

                # ---- PLANNED START DATE ----
                if "PLANNED START DATE" in upper:
                    m = re.search(r"\d{2}-\d{2}-\d{4}", line)
                    if m:
                        planned_start_date = m.group()

                # ---- QUANTITY / WEIGHT ----
                if "TOTAL" in upper and ("QUANTITY" in upper or "WEIGHT" in upper or "ORDER" in upper):
                    m = re.search(r"([\d,]+(\.\d+)?)[\s]*([A-Za-z%/]+)", line)
                    if m:
                        qty = m.group(1).replace(",", "")
                        uom = m.group(3).strip()

            # ---------- Build Final Row ----------
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

                    "ITEM_DESCRIPTION": item_description,
                    "TYPE": final_type,
                    "FIXTURE_CODE": fixture_code,
                    "PLAN_ITEM_DESCRIPTION": plan_item_description,

                    "CATEGORY": category,
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
            data = extract_indent_data(save_path)
            all_indent_data.extend(data)

            file_summary[safe_filename] = {
                "items_extracted": len(data),
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
        "unique_item_codes": len(set(i["ITEM_CODE"] for i in all_indent_data))
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
