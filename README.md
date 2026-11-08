# LogiTrack AI

Reproducible synthetic logistics ETA and delay intelligence platform. This foundation defines the SQLite data model, configuration, documentation, and schema checks. Dataset generation and model training are not included yet.

## Setup

Requires Python 3.12.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## Initialize the database

From the repository root:

```powershell
python -m src.database.init_db --db-path data/logitrack.sqlite
```

The initializer is safe to rerun; it creates missing tables and indexes without deleting existing data. SQLite foreign-key enforcement is enabled on the initialized connection.

## Run foundation tests

```powershell
python -m pytest
```

See [docs/schema.md](docs/schema.md) for relationships and [docs/data_dictionary.md](docs/data_dictionary.md) for field definitions and temporal availability rules.

## Generate the starter dataset

```powershell
python -m src.generator.generate --profile starter --output-dir data/starter
python -m pytest -q
```

The `full` profile is configured for 50,000 shipments, 300,000 tracking events, 100 facilities, and 1,000 disruption windows. Run it only when the starter bundle has passed validation.

## Build point-in-time ETA features

```powershell
python -m src.features.build_features --db-path data/starter/logitrack.sqlite --output-dir data/starter/features
```

See [docs/features.md](docs/features.md) for feature availability rules and target separation. No model is trained by this command.
