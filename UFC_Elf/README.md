# UFC Elf Web Predictor

Production-focused Flask app for UFC fight prediction and bet tracking.

## Project Structure

- `app.py`: Flask routes and bet tracker logic
- `web_predictor.py`: model training/inference service used by the web app
- `scripts/run_ufc_siamese_study.py`: shared model components/utilities required by `web_predictor.py`
- `templates/` + `static/`: frontend
- `data/ufc_fights_rnn.csv`: source dataset for model fitting at startup
- `data/bets_tracker.csv`: local tracker store

## Local Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

App starts on `http://0.0.0.0:8000` by default.

## Environment Variables

- `UFC_DEFAULT_MODEL` (default: `accuracy_weighted_ensemble`)
- `UFC_POWER_PROFILE` (default: `max_power`)
- `SCRAPER_TIMEOUT_SECONDS` (default: `7200`)
- `PORT` (default: `8000`)
- `FLASK_HOST` (default: `0.0.0.0`)
- `FLASK_DEBUG` (default: `0`)
- `UFC_TRAIN_ON_BOOT` (default: `0`) - train models during app startup
- `UFC_FORCE_RETRAIN_ON_BOOT` (default: `1`) - force full base-model retrain on startup when boot training is enabled
- `UFC_TRAIN_SIAMESE_ON_BOOT` (default: `1`) - also train/warm Siamese model during startup when boot training is enabled

## Runtime Behavior

- Base/tabular models are cached to `data/model_cache/base_models.joblib`.
- Siamese weights are cached to `data/model_cache/siamese_no_context.pt` after first Siamese inference.
- By default, restart/deploy loads cached models (no automatic retrain unless cache is missing).
- If `UFC_TRAIN_ON_BOOT=1`, startup performs deployment-time training before serving traffic.
- Use the web UI buttons to:
  - run scraper update (`Update Data`),
  - retrain models on latest data (`Retrain Models`).

## EC2 Deployment

See `deploy/DEPLOY_EC2.md`.
