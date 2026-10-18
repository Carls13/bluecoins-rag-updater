import hashlib
import json
import logging
import os
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3


logger = logging.getLogger()
logger.setLevel(logging.INFO)

ACTIVE_JOB_ID = "active"
EMBEDDING_MODEL_ID = "amazon.titan-embed-text-v2:0"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Required environment variable {name} is not configured.")
    return value


def _table() -> Any:
    return boto3.resource("dynamodb").Table(_required_env("JOB_TABLE_NAME"))


def _secret_bundle() -> dict[str, Any]:
    result = boto3.client("secretsmanager").get_secret_value(
        SecretId=_required_env("CREDENTIALS_SECRET_ARN")
    )
    secret_string = result.get("SecretString")
    if not secret_string:
        raise RuntimeError("The configured Secrets Manager secret must contain JSON.")

    bundle = json.loads(secret_string)
    if not isinstance(bundle.get("google_service_account"), dict):
        raise RuntimeError("Secret JSON must contain a google_service_account object.")
    if not isinstance(bundle.get("pinecone_api_key"), str) or not bundle["pinecone_api_key"]:
        raise RuntimeError("Secret JSON must contain a non-empty pinecone_api_key.")
    return bundle


def _embeddings() -> Any:
    from langchain_aws import BedrockEmbeddings

    return BedrockEmbeddings(
        model_id=EMBEDDING_MODEL_ID,
        region_name=os.environ.get("AWS_REGION"),
        model_kwargs={"dimensions": 1024},
    )


def _response(status_code: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(payload, default=str),
    }


def api_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    method = event.get("httpMethod", "")
    path = event.get("path", "")

    if method == "POST" and path == "/ingest":
        job_id = str(uuid.uuid4())
        created_at = _utc_now()
        _table().put_item(
            Item={
                "jobId": job_id,
                "status": "queued",
                "createdAt": created_at,
                "updatedAt": created_at,
                "expiresAt": int(time.time()) + 30 * 24 * 60 * 60,
            }
        )

        try:
            boto3.client("sqs").send_message(
                QueueUrl=_required_env("INGESTION_QUEUE_URL"),
                MessageBody=json.dumps({"jobId": job_id}),
            )
        except Exception:
            _table().update_item(
                Key={"jobId": job_id},
                UpdateExpression="SET #status = :failed, updatedAt = :updated",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":failed": "failed",
                    ":updated": _utc_now(),
                },
            )
            raise

        return _response(202, {"jobId": job_id, "status": "queued"})

    if method == "GET" and path.startswith("/ingest/"):
        path_parameters = event.get("pathParameters") or {}
        job_id = path_parameters.get("jobId") or path.rsplit("/", 1)[-1]
        result = _table().get_item(Key={"jobId": job_id}, ConsistentRead=True)
        item = result.get("Item")
        if item is None or job_id == ACTIVE_JOB_ID:
            return _response(404, {"error": "Ingestion job not found."})
        payload = {
            "jobId": job_id,
            "status": item["status"],
            "createdAt": item["createdAt"],
            "updatedAt": item["updatedAt"],
        }
        if item["status"] == "failed":
            payload["error"] = item.get("error", "Ingestion failed; check CloudWatch logs.")
        if item.get("cleanupWarning"):
            payload["cleanupWarning"] = item["cleanupWarning"]
        return _response(200, payload)

    if method == "GET" and path == "/search":
        query_parameters = event.get("queryStringParameters") or {}
        query = query_parameters.get("q")
        if not isinstance(query, str) or not query.strip():
            return _response(400, {"error": "Query parameter 'q' is required."})

        try:
            top_k = int(query_parameters.get("k", "5"))
        except (TypeError, ValueError):
            return _response(400, {"error": "Query parameter 'k' must be an integer from 1 to 20."})
        if top_k < 1 or top_k > 20:
            return _response(400, {"error": "Query parameter 'k' must be an integer from 1 to 20."})

        active = _table().get_item(Key={"jobId": ACTIVE_JOB_ID}, ConsistentRead=True).get("Item")
        if active is None:
            return _response(409, {"error": "No transaction CSV has been indexed yet."})

        from langchain_pinecone import PineconeVectorStore

        credentials = _secret_bundle()
        store = PineconeVectorStore(
            index_name=_required_env("PINECONE_INDEX_NAME"),
            embedding=_embeddings(),
            namespace=active["namespace"],
            pinecone_api_key=credentials["pinecone_api_key"],
        )
        matches = store.similarity_search_with_score(query.strip(), k=top_k)
        return _response(
            200,
            {
                "results": [
                    {
                        "text": document.page_content,
                        "score": score,
                        "metadata": document.metadata,
                    }
                    for document, score in matches
                ]
            },
        )

    return _response(404, {"error": "Route not found."})


def _download_drive_database(destination: Path, credentials_bundle: dict[str, Any]) -> str:
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload
    from google.oauth2.service_account import Credentials

    file_id = _required_env("GOOGLE_DRIVE_FILE_ID")
    credentials = Credentials.from_service_account_info(
        credentials_bundle["google_service_account"],
        scopes=[DRIVE_SCOPE],
    )
    drive = build("drive", "v3", credentials=credentials, cache_discovery=False)
    file_info = drive.files().get(fileId=file_id, fields="id,name").execute()
    file_name = file_info["name"]
    if not file_name.lower().endswith(".fydb"):
        raise ValueError("The configured Google Drive file must have a .fydb extension.")

    request = drive.files().get_media(fileId=file_id)
    with destination.open("wb") as file_stream:
        downloader = MediaIoBaseDownload(file_stream, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    return file_name


def _index_transactions(
    csv_path: Path,
    namespace: str,
    file_name: str,
    credentials_bundle: dict[str, Any],
) -> int:
    from langchain_core.documents import Document
    from langchain_pinecone import PineconeVectorStore
    from langchain_text_splitters import RecursiveCharacterTextSplitter
    from unstructured.partition.csv import partition_csv

    elements = partition_csv(filename=str(csv_path))
    documents = [
        Document(page_content=element.text, metadata={"source": file_name})
        for element in elements
        if element.text and element.text.strip()
    ]
    if not documents:
        raise ValueError("The extracted transaction CSV contained no indexable rows.")

    splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=100)
    chunks = splitter.split_documents(documents)

    store = PineconeVectorStore(
        index_name=_required_env("PINECONE_INDEX_NAME"),
        embedding=_embeddings(),
        namespace=namespace,
        pinecone_api_key=credentials_bundle["pinecone_api_key"],
    )
    ids = [
        hashlib.sha256(f"{position}\0{document.page_content}".encode("utf-8")).hexdigest()
        for position, document in enumerate(chunks)
    ]
    store.add_documents(chunks, ids=ids)
    return len(chunks)


def _delete_namespace(namespace: str, pinecone_api_key: str) -> None:
    from pinecone import Pinecone

    client = Pinecone(api_key=pinecone_api_key)
    client.Index(_required_env("PINECONE_INDEX_NAME")).delete(
        delete_all=True,
        namespace=namespace,
    )


def ingestion_worker(event: dict[str, Any], context: Any) -> None:
    table = _table()
    for record in event.get("Records", []):
        message = json.loads(record["body"])
        job_id = message.get("jobId")
        if not isinstance(job_id, str) or not job_id:
            raise ValueError("SQS message is missing a valid jobId.")

        table.update_item(
            Key={"jobId": job_id},
            UpdateExpression="SET #status = :processing, updatedAt = :updated",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":processing": "processing",
                ":updated": _utc_now(),
            },
        )

        namespace = f"bluecoins-{job_id}"
        previous_namespace = None
        credentials_bundle: dict[str, Any] = {}
        try:
            active = table.get_item(Key={"jobId": ACTIVE_JOB_ID}).get("Item")
            if active is not None:
                previous_namespace = active.get("namespace")
            credentials_bundle = _secret_bundle()

            with tempfile.TemporaryDirectory() as temporary_directory:
                from bluecoins_transaction_extractor import extract_all_transactions

                temp_path = Path(temporary_directory)
                database_path = temp_path / "transactions.fydb"
                csv_path = temp_path / "transactions.csv"
                file_name = _download_drive_database(database_path, credentials_bundle)
                transaction_frame = extract_all_transactions(
                    str(database_path),
                    str(csv_path),
                    verbose=False,
                )
                chunk_count = _index_transactions(
                    csv_path,
                    namespace,
                    file_name,
                    credentials_bundle,
                )

            table.put_item(
                Item={
                    "jobId": ACTIVE_JOB_ID,
                    "namespace": namespace,
                    "sourceFile": file_name,
                    "updatedAt": _utc_now(),
                }
            )
            table.update_item(
                Key={"jobId": job_id},
                UpdateExpression=(
                    "SET #status = :completed, updatedAt = :updated, "
                    "transactionCount = :transaction_count, chunkCount = :chunk_count"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":completed": "completed",
                    ":updated": _utc_now(),
                    ":transaction_count": len(transaction_frame),
                    ":chunk_count": chunk_count,
                },
            )
            logger.info(
                "Ingestion job %s indexed %s transactions into namespace %s",
                job_id,
                len(transaction_frame),
                namespace,
            )
        except Exception:
            logger.exception("Ingestion job %s failed", job_id)
            table.update_item(
                Key={"jobId": job_id},
                UpdateExpression=(
                    "SET #status = :failed, updatedAt = :updated, "
                    "error = :error"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":failed": "failed",
                    ":updated": _utc_now(),
                    ":error": "Ingestion failed; check CloudWatch logs.",
                },
            )
            raise
        else:
            if previous_namespace and previous_namespace != namespace:
                try:
                    _delete_namespace(
                        previous_namespace,
                        credentials_bundle["pinecone_api_key"],
                    )
                except Exception:
                    logger.exception(
                        "Ingestion job %s succeeded but old namespace cleanup failed",
                        job_id,
                    )
                    table.update_item(
                        Key={"jobId": job_id},
                        UpdateExpression="SET cleanupWarning = :warning",
                        ExpressionAttributeValues={
                            ":warning": "Previous Pinecone namespace cleanup failed; check CloudWatch logs."
                        },
                    )
