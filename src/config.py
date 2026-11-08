"""Shared deterministic project configuration."""

SEED = 20261022

# Supported generator profiles. Full-volume row targets include exactly six
# tracking records per shipment and the stated disruption/facility quantities.
STARTER_DATASET_SIZE = 100
FULL_DATASET_SIZE = 50_000
FULL_TRACKING_EVENT_COUNT = 300_000
FULL_FACILITY_COUNT = 100
FULL_DISRUPTION_WINDOW_COUNT = 1_000

DEFAULT_DATABASE_PATH = "data/logitrack.sqlite"
