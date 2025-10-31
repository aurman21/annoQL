# annoQL

A lightweight annotation interface for images / text / audio / video that runs **locally** and stores data **locally**.
Useful for research labs, student coders, and privacy-sensitive environments. While the tool supports text annotations, it's especially handy if you need to annotate a lot of files like images or videos and don't want to upload them to some external service/cloud first for privacy, cost-related or other reasons.

It's easy to customize and saves the results locally in a csv file by default.

## Quick Start
1. Clone this repo
   
2. Create a virtual environment & install dependencies (you need to have python installed):

python -m venv .venv
source .venv/bin/activate    # Mac/Linux or for Windows  .\.venv\Scripts\Activate.ps1 
pip install -r requirements.txt

3. To run the app (in terminal):
export FLASK_APP=app.py       # Windows: set FLASK_APP=app.py
flask run --port 5001         #change the port as you need to


**Files you will likely need to edit**
items.csv — the items to annotate (images/text/audio/video).

questions.json — annotation questions (supports help popups).

coders.csv — optional: control which coder works on which items.

config.yaml — batch size, output filename, display settings.
