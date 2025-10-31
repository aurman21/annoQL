import os, csv, json, yaml
from datetime import datetime
from flask import Flask, render_template, request, jsonify, redirect, url_for, session
import pandas as pd
from typing import Optional, Tuple

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret")

# -------------------------
# Load config
# -------------------------
with open("config.yaml", "r", encoding="utf-8") as cf:
    CONFIG = yaml.safe_load(cf)

PROJECT_NAME   = CONFIG.get("project_name", "Annotation")
MEDIA_TYPE     = CONFIG.get("media_type", "image")  # image | text | video | audio
ITEMS_FILE     = CONFIG.get("items_file", "items.csv")
QUESTIONS_FILE = CONFIG.get("questions_file", "questions.json")

BATCH_SIZE   = int(CONFIG.get("batch_size", 5))
SHUFFLE      = bool(CONFIG.get("shuffle_items", True))
ALLOW_REPEAT = bool(CONFIG.get("allow_repeat", False))  # reserved for future use

OUTPUT_CSV = CONFIG.get("output_csv", "ratings.csv")
ALLOW_SKIP = bool(CONFIG.get("allow_skip", False))

PAGE_HEADER_HTML = CONFIG.get("page_header_html", "")
PD           = CONFIG.get("page_description", {}) or {}
PD_ENABLED   = bool(PD.get("enabled", True))
PD_COLUMN    = PD.get("column", "description") or ""
PD_TEMPLATE  = PD.get("template_html", "<h3>{{value}}</h3>")

CODER_MODE   = CONFIG.get("coder_mode", "free_entry")   # "pseudonym" | "free_entry"
CODERS_FILE  = CONFIG.get("coders_file", "coders.csv")
ASSIGN_FILE  = CONFIG.get("assignments_file", "").strip()

# -------------------------
# Helpers
# -------------------------
def ensure_parent_dir(path: str):
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)

# ensure output directory exists on startup
ensure_parent_dir(OUTPUT_CSV)

def read_text_source(src: str) -> str:
    """
    For text projects: if src points to an existing file, read it; else treat src as inline text.
    """
    if isinstance(src, str) and os.path.exists(src) and os.path.isfile(src):
        try:
            with open(src, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            return f"[Error reading file: {src}]"
    return str(src)

def split_ids_field(s: str):
    """
    Split an item_ids string into a set of ids. Accepts separators ';' or ','.
    """
    if not isinstance(s, str) or not s.strip():
        return set()
    s = s.replace(";", ",")
    parts = [p.strip() for p in s.split(",") if p.strip()]
    return set(parts)

def load_assignments_map():
    """
    Returns a dict: coder_id -> set(item_ids) from either assignments_file or coders.csv if present.
    """
    mapping = {}

    # Preferred: dedicated assignments file
    if ASSIGN_FILE and os.path.exists(ASSIGN_FILE):
        try:
            df = pd.read_csv(ASSIGN_FILE)
            if "coder_id" in df.columns and "item_ids" in df.columns:
                for _, r in df.iterrows():
                    coder = str(r["coder_id"]).strip()
                    mapping[coder] = split_ids_field(str(r["item_ids"]))
        except Exception as e:
            print(f"[WARN] Failed to read assignments_file '{ASSIGN_FILE}': {e}")

    # Fallback/also-merge: coders.csv item_ids column (if present)
    if os.path.exists(CODERS_FILE):
        try:
            dfc = pd.read_csv(CODERS_FILE)
            if "coder_id" in dfc.columns and "item_ids" in dfc.columns:
                for _, r in dfc.iterrows():
                    coder = str(r["coder_id"]).strip()
                    ids   = split_ids_field(str(r["item_ids"]))
                    if ids:
                        mapping[coder] = mapping.get(coder, set()) | ids
        except Exception as e:
            print(f"[WARN] Failed to read coders_file '{CODERS_FILE}' for assignments: {e}")

    return mapping

def get_completed_item_ids_for(coder_id: str) -> set:
    """
    Look into OUTPUT_CSV and return the set of item_ids this coder already submitted.
    """
    if not os.path.exists(OUTPUT_CSV) or os.path.getsize(OUTPUT_CSV) == 0:
        return set()
    try:
        df = pd.read_csv(OUTPUT_CSV)
        if "coder_id" not in df.columns or "item_id" not in df.columns:
            return set()
        return set(df[df["coder_id"].astype(str) == str(coder_id)]["item_id"].astype(str).unique())
    except Exception as e:
        print(f"[WARN] Could not read OUTPUT_CSV for progress: {e}")
        return set()

def select_next_group(
    df: pd.DataFrame,
    coder_id: str,
    allowed_item_ids: Optional[set]
) -> Tuple[Optional[str], Optional[pd.DataFrame]]:
    """
    Select the next (item_id, group_df) for this coder:
      - Restrict to allowed_item_ids if provided
      - Exclude item_ids already completed by this coder
      - Shuffle if configured
    """
    work = df.copy()

    # Restrict to assigned item_ids if we have them
    if allowed_item_ids:
        work = work[work["item_id"].astype(str).isin(allowed_item_ids)]

    # Exclude completed
    completed = get_completed_item_ids_for(coder_id)
    if not ALLOW_REPEAT and completed:
        work = work[~work["item_id"].astype(str).isin(completed)]

    if work.empty:
        return None, None

    if SHUFFLE:
        work = work.sample(frac=1, random_state=None)

    # Return the first available group
    for item_id, group in work.groupby("item_id"):
        return str(item_id), group
    return None, None

# -------------------------
# Load questions & items
# -------------------------
with open(QUESTIONS_FILE, "r", encoding="utf-8") as qf:
    QUESTIONS = json.load(qf)

items_df = pd.read_csv(ITEMS_FILE)

# Back-compat: if 'prompt' exists, treat it as description
if "description" not in items_df.columns and "prompt" in items_df.columns:
    items_df = items_df.rename(columns={"prompt": "description"})

# Ensure required columns
if "item_id" not in items_df.columns or "source" not in items_df.columns:
    raise ValueError("items.csv must have at least 'item_id' and 'source' columns.")

# Make sure item_id is string for consistent matching
items_df["item_id"] = items_df["item_id"].astype(str)

# -------------------------
# Coder tracking + assignments
# -------------------------
coders_lookup = set()
coders_df = None
if CODER_MODE == "pseudonym" and os.path.exists(CODERS_FILE):
    coders_df = pd.read_csv(CODERS_FILE)
    if "coder_id" in coders_df.columns:
        coders_lookup = set(coders_df["coder_id"].astype(str))

assignments_map = load_assignments_map()  # coder_id -> set(item_ids)

def allowed_ids_for(coder_id: str) -> Optional[set]:
    ids = assignments_map.get(str(coder_id))
    return ids if ids else None  # None means "no restriction"

# -------------------------
# Routes
# -------------------------
@app.route("/", methods=["GET", "POST"])
def home():
    # Free-entry mode: tiny login form
    if CODER_MODE == "free_entry":
        if request.method == "POST":
            coder_id = request.form.get("coder_id", "").strip()
            if not coder_id:
                return "Coder ID required", 400
            session["coder_id"] = coder_id
            return redirect(url_for("annotate"))
        # Minimal inline login page
        return f"""
        <!DOCTYPE html><html><body style="font-family:Arial;max-width:600px;margin:40px auto">
        <h2>{PROJECT_NAME}</h2>
        <form method="POST">
          <label>Coder ID: <input name="coder_id" required></label>
          <button type="submit">Continue</button>
        </form>
        </body></html>
        """

    # Pseudonym mode: info page
    return f"""
    <!DOCTYPE html><html><body style="font-family:Arial;max-width:700px;margin:40px auto">
      <h2>{PROJECT_NAME}</h2>
      <p>This project uses pseudonym URLs for coders.</p>
      <p>Ask your coordinator for your link, e.g. <code>http://localhost:5000/&lt;your_coder_id&gt;</code>.</p>
    </body></html>
    """

@app.route("/<coder_id>")
def pseudonym_entry(coder_id):
    if CODER_MODE != "pseudonym":
        return "This project is not using pseudonyms.", 400
    if coders_lookup and coder_id not in coders_lookup:
        return "Unauthorized coder ID", 403
    session["coder_id"] = coder_id
    return redirect(url_for("annotate"))

@app.route("/annotate")
def annotate():
    coder_id = session.get("coder_id")
    if not coder_id:
        return redirect(url_for("home"))

    # Optional override: /annotate?n=3
    n_override = request.args.get("n")
    batch_n = int(n_override) if n_override and n_override.isdigit() else BATCH_SIZE

    # Determine allowed item_ids for this coder (None = unrestricted)
    allowed = allowed_ids_for(coder_id)

    # Pick next unseen / allowed group
    item_id, group = select_next_group(items_df, coder_id, allowed)
    if group is None or group.empty:
        # All assigned items completed (or none available)
        return f"""
        <!DOCTYPE html><html><body style="font-family:Arial;max-width:700px;margin:40px auto">
          <h2>{PROJECT_NAME}</h2>
          <p>No more items available for coder <b>{coder_id}</b>.</p>
          <p>If this seems wrong, check your assignments or clear output_csv for re-annotation.</p>
        </body></html>
        """

    # Truncate to batch_n
    group = group.head(batch_n)

    # Prepare optional description block (first non-empty value in chosen column)
    page_desc_html = None
    if PD_ENABLED and PD_COLUMN and PD_COLUMN in group.columns:
        series = group[PD_COLUMN].dropna().astype(str)
        page_desc_value = series.iloc[0].strip() if not series.empty and series.iloc[0].strip() else None
        if page_desc_value:
            page_desc_html = PD_TEMPLATE.replace("{{value}}", page_desc_value)

    # Build items for template (inject display_text if text project)
    items = []
    for _, row in group.iterrows():
        item = row.to_dict()
        if MEDIA_TYPE == "text":
            item["display_text"] = read_text_source(item.get("source", ""))
        items.append(item)

    return render_template(
        "index.html",
        project_name=PROJECT_NAME,
        coder_id=coder_id,
        media_type=MEDIA_TYPE,
        items=items,
        page_header_html=PAGE_HEADER_HTML,
        page_desc_html=page_desc_html,
        questions=QUESTIONS,
        allow_skip=ALLOW_SKIP
    )

@app.route("/submit", methods=["POST"])
def submit():
    coder_id = session.get("coder_id")
    if not coder_id:
        return jsonify({"status": "error", "message": "No coder id"}), 400

    data = request.json or {}
    items_payload = data.get("items", [])
    page_answers = data.get("page_answers", {})
    comments = (data.get("comments") or "").strip()
    timestamp = datetime.now().isoformat()

    # Build rows (one per item)
    rows = []
    for it in items_payload:
        item_row = it.get("item_row", {})
        answers = it.get("answers", {})

        base = {
            "timestamp": timestamp,
            "coder_id": coder_id,
            "media_type": MEDIA_TYPE,
            "item_id": str(item_row.get("item_id")),
            "source": item_row.get("source"),
            "description": item_row.get("description", "")
        }

        # Flatten item-level answers
        for q in QUESTIONS:
            if q.get("applies_to") == "item":
                qid = q["id"]
                val = answers.get(qid, "")
                if isinstance(val, list):
                    val = ",".join(map(str, val))
                base[f"item_{qid}"] = val

        # Add page-level answers
        for q in QUESTIONS:
            if q.get("applies_to") == "page":
                qid = q["id"]
                val = page_answers.get(qid, "")
                if isinstance(val, list):
                    val = ",".join(map(str, val))
                base[f"page_{qid}"] = val

        base["comments"] = comments
        rows.append(base)

    # Write CSV with dynamic header
    fieldnames = sorted({k for row in rows for k in row.keys()})
    new_file = not os.path.exists(OUTPUT_CSV) or os.path.getsize(OUTPUT_CSV) == 0
    with open(OUTPUT_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if new_file:
            writer.writeheader()
        for r in rows:
            writer.writerow(r)

    # Front-end already reloads /annotate, and now annotate() will pick the next available group.
    return jsonify({"status": "success"})
    
if __name__ == "__main__":
    app.run(debug=True)

