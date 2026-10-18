# Bluecoins Drive-to-Pinecone API

This AWS SAM application deploys an IAM-authorized API Gateway REST API and an
asynchronous ingestion worker. The worker downloads the configured Bluecoins
`.fydb` from Google Drive, extracts transactions to a temporary CSV, parses it
with Unstructured's CSV partitioner, chunks its elements with LangChain, and
indexes them in Pinecone using Amazon Bedrock Titan Text Embeddings v2.

Each successful ingestion gets a separate Pinecone namespace. The active
namespace switches only after indexing succeeds; the old namespace is then
removed. `.fydb` and `.csv` files are stored only in Lambda temporary storage.
Before CSV creation and embedding, transactions are limited to the inclusive
rolling six-calendar-month window ending today (UTC in Lambda); older and
future-dated transactions are excluded.

## Deployment

Deployments run from GitHub Actions on pushes to `main` or via
`workflow_dispatch`. The workflow builds Python ZIP packages with AWS SAM on
the GitHub-hosted Linux runner; it does not use Docker.

1. Follow [AWS_DEPLOYMENT.md](./AWS_DEPLOYMENT.md) to set up Google Drive,
   Pinecone, Bedrock, the GitHub OIDC role, and the AWS Secrets Manager secret.
2. Fill in the ignored local `.env` using `.env.example` as a reference.
3. Add the matching values as GitHub repository variables:
   `AWS_REGION`, `AWS_DEPLOY_ROLE_ARN`, `STACK_NAME`,
   `CREDENTIALS_SECRET_ARN`, `GOOGLE_DRIVE_FILE_ID`, and
   `PINECONE_INDEX_NAME`.
4. Run the **Deploy Bluecoins API** workflow or push to `main`.

SAM creates API Gateway, both Lambda functions and their roles, an SQS queue
and dead letter queue, a DynamoDB job table, and an SQS event source mapping.
`sam deploy --resolve-s3` manages the SAM artifact bucket. The deployment role
needs permission to create/update those resources.

## API

- `POST /ingest` — queue a refresh and return `202` with a `jobId`.
- `GET /ingest/{jobId}` — return `queued`, `processing`, `completed`, or
  `failed`. Job records expire after 30 days.
- `GET /search?q=<question>&k=5` — return matching CSV chunks from the active
  Pinecone namespace. `k` must be between 1 and 20.

All routes use AWS IAM authorization and require SigV4-signed requests plus
`execute-api:Invoke` permission. The deployment endpoint is in the CloudFormation
stack outputs. The Pinecone index must have **1024 dimensions** and **cosine**
similarity; the deployment region must have access to
`amazon.titan-embed-text-v2:0`.

For AWS resource creation, IAM setup, GitHub variables, and verification steps,
see [AWS_DEPLOYMENT.md](./AWS_DEPLOYMENT.md).
