#!/bin/bash
source /opt/conda/bin/activate base  # Activate the conda environment
exec python main_agent.py "$@"         # Run main_agent.py with all passed arguments
