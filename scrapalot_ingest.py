#!/usr/bin/env python3
import glob
import os
import sys
from multiprocessing import Pool
from typing import List

from dotenv import set_key
from langchain.docstore.document import Document
from langchain.embeddings import HuggingFaceEmbeddings
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain.vectorstores import Chroma
from tqdm import tqdm

from scripts.app_environment import (
    ingest_chunk_size,
    ingest_chunk_overlap,
    ingest_embeddings_model,
    ingest_persist_directory,
    ingest_source_directory,
    args,
    chromaDB_manager,
    gpu_is_enabled)
from scripts.app_utils import display_directories, LOADER_MAPPING, load_single_document


def load_documents(source_dir: str, ignored_files: List[str] = []) -> List[Document]:
    """
    Loads all documents from the source documents directory, ignoring specified files.
    :param source_dir: The path of the source documents directory.
    :param ignored_files: A list of filenames to be ignored.
    :return: A list of Document objects loaded from the source documents.
    """
    all_files = []
    for ext in LOADER_MAPPING:
        all_files.extend(
            glob.glob(os.path.join(source_dir, f"**/*{ext}"), recursive=True)
        )
    filtered_files = [file_path for file_path in all_files if file_path not in ignored_files]

    with Pool(processes=min(8, os.cpu_count())) as pool:
        results = []
        with tqdm(total=len(filtered_files), desc='Loading new documents', ncols=80) as pbar:
            for i, docs in enumerate(pool.imap_unordered(load_single_document, filtered_files)):
                if isinstance(docs, dict):
                    print(" - " + docs['file'] + ": error: " + str(docs['exception']))
                    continue
                for d in docs:
                    print(f"\n\033[32m\033[2m\033[38;2;0;128;0m{d.metadata.get('source', '')} \033[0m")
                results.extend(docs)
                pbar.update()

    return results


def process_documents(ignored_files: List[str] = []) -> List[Document]:
    """
    Load documents and split in chunks
    """
    print(f"Loading documents from {source_directory}")
    documents = load_documents(source_directory, ignored_files)
    if not documents:
        print("No new documents to load")
        exit(0)
    print(f"Loaded {len(documents)} new documents from {source_directory}")
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=ingest_chunk_size if ingest_chunk_size else args.ingest_chunk_size,
        chunk_overlap=ingest_chunk_overlap if ingest_chunk_overlap else args.ingest_chunk_overlap
    )  # find double new-line, if not then dot single, if not then dot, ... otherwise anything
    texts = text_splitter.split_documents(documents)
    print(f"Split into {len(texts)} chunks of text (max. {ingest_chunk_size} tokens each)")
    return texts


def does_vectorstore_exist(persist_directory: str) -> bool:
    """
    Checks if a Chroma vectorstore already exists in the given directory.
    :param persist_directory: The path of the vectorstore directory.
    :return: True if the vectorstore exists, False otherwise.
    """
    if os.path.exists(os.path.join(persist_directory, 'index')):
        if os.path.exists(os.path.join(persist_directory, 'chroma-collections.parquet')) and os.path.exists(os.path.join(persist_directory, 'chroma-embeddings.parquet')):
            list_index_files = glob.glob(os.path.join(persist_directory, 'index/*.bin'))
            list_index_files += glob.glob(os.path.join(persist_directory, 'index/*.pkl'))
            # At least 3 documents are needed in a working vectorstore
            if len(list_index_files) > 3:
                return True
    return False


def tag_collection(database_name: str, db_collection_name: str):
    """
    Prompts the user to rename the default collection name in the Chroma database.
    :param database_name: The name of the Chroma database.
    :param db_collection_name: The desired collection name.
    """
    client = chromaDB_manager.get_client(database_name)
    for col in client.list_collections():
        print(f"Existing collection: {col}")

    try:
        default_collection = client.get_collection(name='langchain')
        if default_collection:
            client.delete_collection(name='langchain')

        collection = client.get_collection(name=db_collection_name)
        if collection:
            client.get_or_create_collection(name=db_collection_name)
    except TypeError:
        print("TypeError occurred")
    except ValueError as ve:
        print(f"ValueError occurred: {ve}")
        client.get_or_create_collection(name=db_collection_name)
        pass
    except Exception as e:
        print("Some other exception occurred: ", str(e))


def prompt_user():
    """
    Prompts the user to select an existing directory or create a new one to store source material.
    If an existing directory is selected, it checks if the directory is empty and prompts the user to create files
    in the directory if it is empty. It sets the directory paths as environment variables and returns them.
    :return: The selected source directory path, the selected database directory path, and the collection name.
    """

    def _create_directory(directory_name):
        """
        Creates a new directory with the given directory_name in the ./source_documents directory.
        It also creates a corresponding directory in the ./db directory for the database files.
        It sets the directory paths as environment variables and returns them.
        :param directory_name: The name for the new directory.
        :return: The path of the new directory and the path of the database directory.
        """
        directory_path = f"./source_documents/{directory_name}"
        db_path = f"./db/{directory_name}"
        os.makedirs(directory_path)
        os.makedirs(db_path)
        set_key('.env', 'INGEST_SOURCE_DIRECTORY', directory_path)
        set_key('.env', 'INGEST_PERSIST_DIRECTORY', db_path)
        print(f"Created new directory: {directory_path}")
        return directory_path, db_path

    while True:
        print(f"\033[94mSelect an option or 'q' to quit:\n\033[0m")
        print("1. Select an existing directory")
        print("2. Create a new directory")
        print(f"3. Use current ingest_source_directory: {ingest_source_directory}")

        user_choice = input('\nEnter your choice ("q" for quit): ').strip()

        if user_choice == "1":
            directories = display_directories()
            while True:  # Keep asking until we get a valid directory number
                existing_directory = input("\n\033[94mEnter the number of the existing directory (q for quit, b for back): \033[0m")
                if existing_directory == 'q':
                    raise SystemExit
                elif existing_directory == 'b':
                    break
                try:
                    selected_directory = directories[int(existing_directory) - 1]
                    selected_directory_path = f"./source_documents/{selected_directory}"
                    selected_db_path = f"./db/{selected_directory}"
                    if not os.listdir(selected_directory_path):
                        print(f"\033[91m\033[1m[!]\033[0m Selected directory: '{selected_directory}' is empty \033[91m\033[1m[!]\033[0m")
                        directories = display_directories()  # Display directories again if the selected one is empty
                    else:
                        if not os.path.exists(selected_db_path):
                            os.makedirs(selected_db_path)
                        set_key('.env', 'INGEST_SOURCE_DIRECTORY', selected_directory_path)
                        set_key('.env', 'INGEST_PERSIST_DIRECTORY', selected_db_path)
                        print(f"Selected directory: {selected_directory_path}")
                        return selected_directory_path, selected_db_path
                except (ValueError, IndexError):
                    print("\n\033[91m\033[1m[!] \033[0mInvalid choice. Please try again.\033[91m\033[1m[!] \033[0m\n")
                    directories = display_directories()  # Display directories again if the input is invalid
        elif user_choice == "2":
            new_directory_name = input("Enter the name for the new directory: ")
            selected_directory_path, selected_db_path = _create_directory(new_directory_name)
            input("Place your source material into the new folder and press enter to continue...")
            return selected_directory_path, selected_db_path
        elif user_choice == "3":
            return ingest_source_directory, ingest_persist_directory
        elif user_choice == "q":
            exit(0)
        else:
            print("\n\033[91m\033[1m[!] \033[0mInvalid choice. Please try again.\033[91m\033[1m[!] \033[0m\n")


def main(source_dir: str, persist_dir: str, db_collection_name: str):
    # Create embeddings
    embeddings_kwargs = {'device': 'cuda'} if gpu_is_enabled else {}
    embeddings = HuggingFaceEmbeddings(
        model_name=ingest_embeddings_model if ingest_embeddings_model else args.ingest_embeddings_model,
        model_kwargs=embeddings_kwargs
    )

    if does_vectorstore_exist(persist_dir):
        # Update and store locally vectorstore
        print(f"Appending to existing vectorstore at {persist_dir}")

        db = Chroma(
            persist_directory=persist_dir,
            collection_name=db_collection_name,
            embedding_function=embeddings,
            client_settings=chromaDB_manager.get_chroma_setting(persist_dir)
        )
        collection = db.get()
        texts = process_documents([metadata['source'] for metadata in collection['metadatas']])
        num_elements = len(texts)  # Calculate the total number of documents in texts
        index_metadata = {"elements": num_elements}  # Provide the "elements" key
        print(f"Creating embeddings. May take some minutes...")
        db.add_documents(texts, index_metadata=index_metadata)
    else:
        # Create and store locally vectorstore
        print(f"Creating new vectorstore from {source_dir}")
        texts = process_documents([source_dir])
        num_elements = len(texts)  # Calculate the total number of documents in texts
        index_metadata = {"elements": num_elements}  # Provide the "elements" key
        db = Chroma.from_documents(
            texts,
            embeddings,
            persist_directory=persist_dir,
            collection_name=db_collection_name,
            client_settings=chromaDB_manager.get_chroma_setting(persist_dir),
            index_metadata=index_metadata
        )
    db.persist()
    db = None

    print("Ingestion complete! You can now run scrapalot_main.py to query your documents")


if __name__ == "__main__":
    try:
        if args.ingest_dbname:
            source_directory = f"./source_documents/{args.ingest_dbname}"
            persist_directory = f"./db/{args.ingest_dbname}"

            if not os.path.exists(source_directory):
                os.makedirs(source_directory)

            if not os.path.exists(persist_directory):
                os.makedirs(persist_directory)

            if args.collection:
                collection_name = args.collection
                # tag_collection(args.ingest_dbname, args.collection)
            else:
                collection_name = args.ingest_dbname
                # tag_collection(args.ingest_dbname, args.ingest_dbname)

            main(source_directory, persist_directory, collection_name)
        else:
            source_directory, persist_directory = prompt_user()
            db_name = os.path.basename(persist_directory)
            # tag_collection(db_name, db_name)
            main(source_directory, persist_directory, db_name)
    except SystemExit:
        print("\n\033[91m\033[1m[!] \033[0mExiting program! \033[91m\033[1m[!] \033[0m")
        sys.exit(1)
