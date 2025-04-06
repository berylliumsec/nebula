
from chroma_manager import ChromaManager

# Initialize the manager (with your desired collection name and persist directory)
manager = ChromaManager(
    collection_name="nebula_collection", persist_directory="/home/agent/nebula/my_chroma_db"
)

# Load documents from a JSON file (or any other supported source)
# docs = manager.load_documents("/home/agent/gemma-nebula/data/nebula_gemma_large.jsonl", source_type="jsonl")
url_docs = manager.load_documents(
    "https://www.berylliumsec.com/dap-overview", source_type="url"
)
# Optionally add other documents from different sources
# additional_docs = manager.load_documents("data/another_file.pdf", source_type="pdf")
# all_docs = docs + additional_docs

# # Add all documents to the vector store
# manager.add_documents(all_docs)

for i, doc in enumerate(url_docs, start=1):
    print(f"Document {i} content:\n{doc.page_content}\n")
    print(f"Metadata: {doc.metadata}\n")
