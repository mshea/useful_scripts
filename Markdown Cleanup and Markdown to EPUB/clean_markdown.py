#!/usr/bin/env python3
"""
Clean markdown files from markdownload format.

This script:
- Adds chapter numbers based on file timestamps (oldest = chapter 1)
- Cleans filenames (removes redundant prefixes)
- Cleans heading (removes redundant text)
- Removes extra blank lines
- Downloads images to local "images" directory
- Removes all external links (keeps text, removes URL)
- Cleans up markdownload artifacts

License:
    CC0 1.0 Universal (CC0 1.0) Public Domain Dedication

    To the extent possible under law, the author(s) have dedicated all
    copyright and related and neighboring rights to this software to the
    public domain worldwide. This software is distributed without any warranty.

    See <http://creativecommons.org/publicdomain/zero/1.0/>
"""

import os
import re
import urllib.request
import hashlib
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse


def get_files_by_timestamp(source_dir):
    """Get all .md files sorted by modification time (oldest first)."""
    md_files = []
    for root, dirs, files in os.walk(source_dir):
        for file in files:
            if file.endswith('.md'):
                filepath = os.path.join(root, file)
                mtime = os.path.getmtime(filepath)
                md_files.append((filepath, mtime))

    # Sort by timestamp (oldest first)
    md_files.sort(key=lambda x: x[1])
    return [f[0] for f in md_files]


def find_common_prefix(filenames):
    """Find common prefix in a list of filenames."""
    if not filenames:
        return ""

    # Remove .md extension for analysis
    names = [f.replace('.md', '') for f in filenames]

    if not names:
        return ""

    # Find the common prefix by comparing characters
    # Start with the first filename
    prefix = names[0]

    for name in names[1:]:
        # Find where the current prefix and this name diverge
        i = 0
        while i < len(prefix) and i < len(name) and prefix[i] == name[i]:
            i += 1
        prefix = prefix[:i]

        if not prefix:
            break

    # Clean up the prefix to end at a word boundary (after " - ")
    if ' - ' in prefix:
        # Find the last occurrence of ' - ' in the prefix
        last_separator = prefix.rfind(' - ')
        if last_separator > 0:
            prefix = prefix[:last_separator + 3]  # Include the ' - '

    return prefix if len(prefix) > 3 else ""


def find_common_suffix(filenames):
    """Find common suffix in a list of filenames."""
    if not filenames:
        return ""

    # Remove .md extension for analysis
    names = [f.replace('.md', '') for f in filenames]

    if not names:
        return ""

    # Find the common suffix by comparing characters from the end
    suffix = names[0]

    for name in names[1:]:
        # Find where the current suffix and this name diverge (from the end)
        i = 1
        while i <= len(suffix) and i <= len(name) and suffix[-i] == name[-i]:
            i += 1
        suffix = suffix[-(i-1):] if i > 1 else ""

        if not suffix:
            break

    # Clean up the suffix to start at a word boundary (before " - ")
    if ' - ' in suffix:
        # Find the first occurrence of ' - ' in the suffix
        first_separator = suffix.find(' - ')
        if first_separator >= 0:
            suffix = suffix[first_separator:]  # Include the ' - '

    return suffix if len(suffix) > 3 else ""


def clean_filename(original_filename, common_prefix="", common_suffix=""):
    """Remove redundant prefixes and suffixes from filename."""
    # Remove .md extension first
    cleaned = original_filename.replace('.md', '')

    # Remove the common prefix if provided
    if common_prefix and cleaned.startswith(common_prefix):
        cleaned = cleaned[len(common_prefix):]

    # Remove the common suffix if provided
    if common_suffix and cleaned.endswith(common_suffix):
        cleaned = cleaned[:-len(common_suffix)]

    # Add .md extension back
    cleaned = cleaned.strip() + '.md'

    return cleaned


def download_image(url, images_dir):
    """Download an image and return the local filename."""
    try:
        # Create a unique filename based on URL hash
        url_hash = hashlib.md5(url.encode()).hexdigest()[:12]

        # Try to get file extension from URL
        parsed = urlparse(url)
        path = parsed.path
        ext = os.path.splitext(path)[1]

        # If no extension, try to get from content-type
        if not ext:
            ext = '.jpg'  # default

        filename = f"image_{url_hash}{ext}"
        filepath = images_dir / filename

        # Download if not already present
        if not filepath.exists():
            print(f"    Downloading: {url}")
            urllib.request.urlretrieve(url, filepath)

        return filename
    except Exception as e:
        print(f"    Error downloading {url}: {e}")
        return None


def clean_content(content, title, images_dir):
    """Clean markdown content."""
    lines = content.split('\n')
    cleaned_lines = []

    # Track if we need to replace the first heading
    found_first_heading = False

    for i, line in enumerate(lines):
        # Replace the first heading with cleaned version
        if not found_first_heading and line.strip().startswith('# '):
            cleaned_lines.append(f'# {title}')
            found_first_heading = True
            continue

        # Skip completely empty lines at the start
        if not cleaned_lines and not line.strip():
            continue

        # First, handle nested markdownload format: [![](images/file)](http://url)
        # This extracts the actual image URL from the outer link
        def replace_nested_image(match):
            image_markdown = match.group(1)  # The inner ![](path) part
            outer_url = match.group(2)  # The actual image URL

            # Extract alt text from inner image (if any)
            alt_match = re.search(r'!\[(.*?)\]', image_markdown)
            alt_text = alt_match.group(1) if alt_match else ""

            # Download from the outer URL
            if outer_url.startswith('http'):
                local_filename = download_image(outer_url, images_dir)
                if local_filename:
                    return f"![{alt_text}](images/{local_filename})"
                else:
                    return alt_text
            return match.group(0)

        # Process nested image links first
        line = re.sub(r'\[(!\[.*?\]\(.*?\))\]\((https?://[^\)]+)\)', replace_nested_image, line)

        # Process regular images: ![alt](url)
        def replace_image(match):
            alt_text = match.group(1)
            url = match.group(2)

            # If it's already a local path, keep it
            if not url.startswith('http'):
                return match.group(0)

            # Download the image
            local_filename = download_image(url, images_dir)
            if local_filename:
                # Return relative path
                return f"![{alt_text}](images/{local_filename})"
            else:
                # If download failed, just keep alt text
                return alt_text

        line = re.sub(r'!\[(.*?)\]\((.*?)\)', replace_image, line)

        # Handle remaining links: download if image, otherwise remove but keep text
        def replace_link(match):
            text = match.group(1)
            url = match.group(2)

            # If it's already a local reference (like #anchor), keep it
            if url.startswith('#'):
                return match.group(0)

            # Check if the URL points to an image
            if url.startswith('http') and any(url.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg']):
                # Download the image
                local_filename = download_image(url, images_dir)
                if local_filename:
                    return f"![{text}](images/{local_filename})"
                else:
                    return text

            # Otherwise, just return the text (remove the link)
            return text

        # Use negative lookbehind to skip image markdown (don't match if preceded by !)
        line = re.sub(r'(?<!!)\[(.*?)\]\((.*?)\)', replace_link, line)

        cleaned_lines.append(line)

    # Join lines and normalize blank lines (max 2 consecutive)
    content = '\n'.join(cleaned_lines)
    content = re.sub(r'\n\n\n+', '\n\n', content)

    # Ensure file ends with single newline
    content = content.rstrip() + '\n'

    return content


def clean_markdown_files(book_dir, dest_dir):
    """Process all markdown files from a specific book directory."""
    dest_path = Path(dest_dir)

    print(f"\nProcessing book: {book_dir.name}")

    # Get files sorted by timestamp
    files = get_files_by_timestamp(str(book_dir))

    if not files:
        print(f"  No markdown files found in {book_dir.name}")
        return False

    # Find common prefix and suffix in all filenames
    filenames = [Path(f).name for f in files]
    common_prefix = find_common_prefix(filenames)
    common_suffix = find_common_suffix(filenames)

    if common_prefix:
        print(f"  Removing common prefix: '{common_prefix.rstrip(' - ')}'")
    if common_suffix:
        print(f"  Removing common suffix: '{common_suffix.lstrip(' - ')}'")

    # Create destination directory
    dest_path.mkdir(parents=True, exist_ok=True)

    # Create images directory
    images_dir = dest_path / 'images'
    images_dir.mkdir(exist_ok=True)

    # Process each file
    for chapter_num, filepath in enumerate(files, start=1):
        original_filename = Path(filepath).name

        # Clean the filename
        cleaned_name = clean_filename(original_filename, common_prefix, common_suffix)
        title = cleaned_name.replace('.md', '')

        # Format with chapter number (zero-padded to 2 digits)
        new_filename = f"{chapter_num:02d} - {cleaned_name}"

        # Read original content
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()

        # Clean content
        cleaned_content = clean_content(content, title, images_dir)

        # Write to destination
        dest_file = dest_path / new_filename
        with open(dest_file, 'w', encoding='utf-8') as f:
            f.write(cleaned_content)

        print(f"  [{chapter_num:02d}] {original_filename} -> {new_filename}")

    print(f"  Processed {len(files)} files")
    return True


def main():
    """Main entry point."""
    import sys

    # Check if input and output directories were provided
    if len(sys.argv) < 3:
        print("Error: Please specify input and output directories.")
        print("\nUsage: python3 clean_markdown.py <input_dir> <output_dir>")
        print("\nExample:")
        print("  python3 clean_markdown.py original_markdown/my-book clean_markdown/my-book")
        return 1

    input_dir = Path(sys.argv[1])
    output_dir = Path(sys.argv[2])

    if not input_dir.exists():
        print(f"Error: Input directory not found: {input_dir}")
        return 1

    if not input_dir.is_dir():
        print(f"Error: Input path is not a directory: {input_dir}")
        return 1

    print(f"\n{'='*60}")
    print(f"Cleaning markdown for: {input_dir.name}")
    print(f"{'='*60}")
    print(f"Source: {input_dir}")
    print(f"Destination: {output_dir}")

    success = clean_markdown_files(input_dir, output_dir)

    if not success:
        print(f"Failed to clean markdown for {input_dir.name}")
        return 1

    print(f"\n{'='*60}")
    print("✓ Cleaning complete!")
    print(f"{'='*60}")

    return 0


if __name__ == "__main__":
    exit(main())
