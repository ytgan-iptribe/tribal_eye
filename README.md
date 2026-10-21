## Main Files: 
FrontEnd:
- demo/frontend


Backends: 
- demo/simulate_api_server.py (all the APIs / SSEs used by the frontend are registered in this Flask server file)
- demo/classification_worker.py (this reads from a sqlite file for the queued data for ML Classification)
- demo/core/verification/simulate_workflow_verification.py (this also reads from sqlite file for OpenSearch Verification)
- demo/modem_worker.py (also read from sqlite file)
