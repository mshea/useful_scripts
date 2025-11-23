#!/usr/bin/env python3
"""
Build EPUB from cleaned markdown files.

This script:
- Converts cleaned markdown files to EPUB format
- Generates EPUB with each markdown file as a chapter
- Includes any local images from the images/ directory

License:
    CC0 1.0 Universal (CC0 1.0) Public Domain Dedication

    To the extent possible under law, the author(s) have dedicated all
    copyright and related and neighboring rights to this software to the
    public domain worldwide. This software is distributed without any warranty.

    See <http://creativecommons.org/publicdomain/zero/1.0/>
"""

import os
import re
from pathlib import Path
import mimetypes


def find_first_image(markdown_content):
    """Find the first image reference in markdown content."""
    # Match image markdown syntax: ![alt](path)
    match = re.search(r'!\[.*?\]\((.*?)\)', markdown_content)
    if match:
        img_path = match.group(1)
        # Extract just the filename if it's a relative path
        if 'images/' in img_path:
            return img_path.split('images/')[-1]
        return img_path
    return None


def create_epub(book_dir, output_file, book_title=None):
    """Create an EPUB file from markdown chapters."""
    try:
        import markdown
        from ebooklib import epub
    except ImportError:
        print("\nError: Required libraries not installed.")
        print("Please install them with:")
        print("  pip install markdown ebooklib")
        return False

    book_path = Path(book_dir)

    # Use directory name as book title if not provided
    if not book_title:
        book_title = book_path.name

    # Create EPUB book
    book = epub.EpubBook()
    book.set_identifier(f'id_{book_title.replace(" ", "_")}')
    book.set_title(book_title)
    book.set_language('en')
    book.add_author('Unknown')  # Can be customized

    # Get all markdown files sorted by name (they're already numbered)
    md_files = sorted(book_path.glob('*.md'))

    if not md_files:
        print(f"Error: No markdown files found in {book_dir}")
        return False

    chapters = []
    images_dir = book_path / 'images'
    cover_image_name = None

    # Check for cover image with common extensions
    for ext in ['.webp', '.png', '.jpg', '.jpeg']:
        cover_path = images_dir / f'cover{ext}'
        if cover_path.exists():
            cover_image_name = f'cover{ext}'
            print(f"Using cover image: {cover_image_name}")
            break

    # If no cover image found, search all markdown files for first image
    if not cover_image_name and md_files:
        for md_file in md_files:
            with open(md_file, 'r', encoding='utf-8') as f:
                content = f.read()
            cover_image_name = find_first_image(content)
            if cover_image_name:
                print(f"Using cover image from markdown: {cover_image_name}")
                break

    # If still no cover, use first image file in images directory
    if not cover_image_name and images_dir.exists():
        image_files = sorted([f for f in images_dir.iterdir() if f.is_file() and f.suffix.lower() in ['.jpg', '.jpeg', '.png', '.webp']])
        if image_files:
            cover_image_name = image_files[0].name
            print(f"Using first image as cover: {cover_image_name}")

    # Process each markdown file as a chapter
    for i, md_file in enumerate(md_files, start=1):
        print(f"\nProcessing chapter {i}: {md_file.name}")

        with open(md_file, 'r', encoding='utf-8') as f:
            content = f.read()

        # Convert markdown to HTML
        html_content = markdown.markdown(content, extensions=['tables', 'fenced_code'])

        # Create chapter
        chapter = epub.EpubHtml(
            title=md_file.stem,  # Filename without extension
            file_name=f'chapter_{i:02d}.xhtml',
            lang='en'
        )
        chapter.content = html_content

        # Add chapter to book
        book.add_item(chapter)
        chapters.append(chapter)

    # Add images to EPUB
    cover_image_data = None
    cover_image_ext = None
    for img_file in images_dir.glob('*'):
        if img_file.is_file():
            with open(img_file, 'rb') as f:
                img_data = f.read()

            # Determine media type
            media_type = mimetypes.guess_type(img_file.name)[0]
            if not media_type:
                media_type = 'image/jpeg'  # default

            img_item = epub.EpubItem(
                uid=img_file.stem,
                file_name=f'images/{img_file.name}',
                media_type=media_type,
                content=img_data
            )
            book.add_item(img_item)

            # Save cover image data if this is the cover
            if cover_image_name and img_file.name == cover_image_name:
                cover_image_data = img_data
                cover_image_ext = img_file.suffix

    # Set cover image if found
    if cover_image_data and cover_image_ext:
        book.set_cover(f'cover{cover_image_ext}', cover_image_data)

    # Define Table of Contents
    book.toc = chapters

    # Add navigation files
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    # Define spine (reading order)
    book.spine = ['nav'] + chapters

    # Write EPUB file
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    epub.write_epub(output_path, book)
    print(f"\n✓ EPUB created: {output_path}")

    return True


def main():
    """Main entry point."""
    import sys

    # Get the script's directory
    script_dir = Path(__file__).parent.parent
    clean_markdown_dir = script_dir / "clean_markdown"

    if not clean_markdown_dir.exists():
        print(f"Error: Clean markdown directory not found: {clean_markdown_dir}")
        print("Please run clean_markdown.py first.")
        return 1

    # Check if book name was provided as argument
    if len(sys.argv) < 2:
        print("Error: Please specify which book to build.")
        print("\nUsage: python3 build_epub.py <book_name>")
        print("\nAvailable books:")
        book_dirs = [d for d in clean_markdown_dir.iterdir() if d.is_dir() and not d.name.startswith('.')]
        for d in sorted(book_dirs):
            print(f"  - {d.name}")
        return 1

    book_name = sys.argv[1]
    book_dir = clean_markdown_dir / book_name

    if not book_dir.exists() or not book_dir.is_dir():
        print(f"Error: Book directory not found: {book_dir}")
        print("\nAvailable books:")
        book_dirs = [d for d in clean_markdown_dir.iterdir() if d.is_dir() and not d.name.startswith('.')]
        for d in sorted(book_dirs):
            print(f"  - {d.name}")
        return 1

    print(f"\n{'='*60}")
    print(f"Building EPUB for: {book_dir.name}")
    print(f"{'='*60}")

    output_file = script_dir / f"{book_dir.name}.epub"

    success = create_epub(book_dir, output_file, book_title=book_dir.name)

    if not success:
        print(f"Failed to create EPUB for {book_dir.name}")
        return 1

    print(f"\n{'='*60}")
    print("✓ EPUB created successfully!")
    print(f"{'='*60}")

    return 0


if __name__ == "__main__":
    exit(main())
