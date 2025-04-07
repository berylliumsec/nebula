#!/usr/bin/env python3
"""
list_chroma_db.py

A script to load the Chroma database and list its content.
"""

from chroma_manager import ChromaManager


def main():
    # Set your collection name and persist directory as needed.
    collection_name = "nebula_collection"
    persist_directory = "/home/agent/nebula/my_chroma_db"

    print(
        f"Loading ChromaDB from '{persist_directory}' with collection '{collection_name}'..."
    )

    # Initialize the ChromaManager.
    manager = ChromaManager(
        collection_name=collection_name, persist_directory=persist_directory
    )

    # Retrieve the collection data from the underlying vector store.
    collection_data = manager.vector_store._collection.get()

    # Get the number of items (assuming the returned dict contains an "ids" key).
    ids = collection_data.get("ids", [])
    num_items = len(ids)
    print(f"ChromaDB collection '{collection_name}' contains {num_items} items.")

    # Optionally, iterate over each item and print its details.
    documents = collection_data.get("documents", [])
    metadatas = collection_data.get("metadatas", [])

    for idx, doc_id in enumerate(ids):
        print(f"\nItem {idx + 1}:")
        print(f"ID: {doc_id}")
        if idx < len(documents):
            print(f"Document: {documents[idx]}")
        if idx < len(metadatas):
            print(f"Metadata: {metadatas[idx]}")


if __name__ == "__main__":
    main()
