# backend.py
import pdfplumber
import json
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
import os

# ---------- Flask App ----------
app = Flask(__name__)
CORS(app)  # Allow all origins for development. Change to specific origins in production.

OUTPUT_JSON = "indent_data.json"
UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


# ---------- Extraction Logic ----------
def extract_indent_data(pdf_path):
    rows = []
    upload_time = datetime.now().strftime("%d-%m-%Y %H:%M:%S")

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if not text:
                continue

            lines = text.split("\n")
            item_code, qty, uom = None, None, None

            for line in lines:
                if "ITEM CODE" in line.upper():
                    item_code = line.split()[-1].strip()

                if "TOTAL ORDER QUANTITY" in line.upper() and ":" in line:
                    qty_part = line.split(":", 1)[1].strip()
                    parts = qty_part.split()
                    qty = parts[0]
                    if len(parts) > 1:
                        uom = parts[1]

            if item_code and qty and uom:
                try:
                    qty_val = float(qty)
                except:
                    qty_val = qty

                rows.append({
                    "ITEM_CODE": item_code,
                    "REQUIRED_QTY": qty_val,
                    "UOM": uom,
                    "DATE_OF_UPLOAD": upload_time,
                    "SOURCE_FILE": os.path.basename(pdf_path)
                })
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
        save_path = os.path.join(UPLOAD_FOLDER, f.filename)
        f.save(save_path)

        try:
            indent_data = extract_indent_data(save_path)
            all_indent_data.extend(indent_data)

            file_summary[f.filename] = {
                "items_extracted": len(indent_data),
                "status": "Success"
            }
        except Exception as e:
            file_summary[f.filename] = {
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
