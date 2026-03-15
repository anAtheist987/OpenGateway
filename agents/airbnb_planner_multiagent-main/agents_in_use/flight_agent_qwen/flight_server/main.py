# Copyright 2026 Tsinghua University
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# This file was created by Tsinghua University and is not part of
# the original agentgateway project by Solo.io.

import os
from dotenv import load_dotenv
from flight_server import mcp

def main():
    # Load environment variables from the flight_agent's .env file
    # This ensures the server can access API keys even when run independently
    env_path = os.path.join(os.path.dirname(__file__), '..', '.env')
    
    # Check if the env file exists
    if os.path.exists(env_path):
        load_dotenv(env_path)
        print(f"Loaded environment variables from: {env_path}")
    else:
        print(f"Warning: .env file not found at {env_path}")
        print("Attempting to use environment variables from parent process...")
    
    mcp.run(transport='stdio')


if __name__ == "__main__":
    main()
