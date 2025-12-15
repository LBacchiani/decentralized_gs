#!/bin/bash

# Usage: ./run_abs.sh <path_to_adaptation_technique> <path_to_system> 

# Check for required arguments
if [ "$#" -ne 2 ]; then
  echo "Usage: $0 <path_to_adaptation_technique> <path_to_system>"
  exit 1
fi

ADAPTATION="$1"
SYS="$2"


java -jar absfrontend.jar --erlang "$ADAPTATION"/*.abs "$ADAPTATION/$SYS"/*.abs "$ADAPTATION/$SYS"/orchestrations/*.abs
#java -jar absfrontend.jar --java "$ADAPTATION"/*.abs "$ADAPTATION/$SYS"/*.abs "$ADAPTATION/$SYS"/orchestrations/*.abs -o model.jar