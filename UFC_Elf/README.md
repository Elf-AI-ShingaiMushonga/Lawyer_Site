# UFC Module (Embedded)

UFC prediction and bet-tracker module embedded in the main law intranet app.

## Project Structure

- `app.py`: UFC blueprint routes and bet tracker logic (mounted at `/ufc`)
- `web_predictor.py`: model training/inference service used by the web app
- `scripts/run_ufc_siamese_study.py`: shared model components/utilities required by `web_predictor.py`
- `templates/` + `static/`: frontend
- `data/ufc_fights_rnn.csv`: source dataset
- `data/bets_tracker.csv`: local tracker store

## Local Run (from repo root)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py run
```

Then open `http://127.0.0.1:5000/ufc/`.

## Environment Variables

- `UFC_DEFAULT_MODEL` (default: `accuracy_weighted_ensemble`)
- `UFC_POWER_PROFILE` (default: `max_power`)
- `SCRAPER_TIMEOUT_SECONDS` (default: `7200`)

## Runtime Behavior

- Base/tabular models are cached to `data/model_cache/base_models.joblib`.
- Siamese weights are cached to `data/model_cache/siamese_no_context.pt`.
- Startup does not train models.
- If no trained cache exists yet, click `Retrain Models` in the UFC UI before predicting.
- Use the web UI buttons to:
  - run scraper update (`Update Data`),
  - retrain models on latest data (`Retrain Models`).
