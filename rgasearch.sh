#!/bin/bash

while true; do
  # Prompt for a search query
  query=$(echo "" | fzf --print-query --prompt "Enter search query: " --layout=reverse | head -n 1)

  # Search for the query in PDFs, count matches per file, and sort them
  file=$(rga --type pdf "$query" . 2>/dev/null | awk -F: '{print $1}' | sort | uniq -c | sort -rn | awk '{$1=""; print substr($0,2)}' | fzf --delimiter : --preview "rga --pretty --context 5 '$query' {1} 2>/dev/null | head -200" --preview-window=right:60% --layout=reverse)

  # Check if a file was selected
  if [ -n "$file" ]; then
    open "$file"
  else
    echo "No file selected."
  fi
done
