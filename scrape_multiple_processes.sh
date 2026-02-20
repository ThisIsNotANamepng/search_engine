#!/bin/bash

# Function to start a single background process
start_process() {
    python3 scrape.py
}

source env/bin/activate

# Loop to start 12 processes
for i in {1..12}; do
    start_process &
    echo "Started process $i"
done

echo "All 12 processes started in the background."

wait
