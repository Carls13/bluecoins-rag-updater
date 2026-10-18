# AWS and GitHub Actions setup

This guide provisions external credentials first, then lets the GitHub Actions
workflow deploy the AWS application stack. The workflow uses AWS SAM's native
Python ZIP build on Ubuntu and does not require Docker.

## 1. Prepare Google Drive access

1. In Google Cloud, create or select a project and enable the **Google Drive
   API**.
2. Create a service account and download its JSON key.
3. Share the Bluecoins `.fydb` file in Drive with the service account's
   `client_email`, granting at least Viewer access.
4. Copy the file ID from its Drive URL. The ID is the value between `/d/` and
   `/edit` (or the `id` query parameter for some Drive URLs).

Keep the service-account JSON private; it will be stored in Secrets Manager,
not in GitHub or this repository.

## 2. Create the Pinecone index

In Pinecone, create an index with:

- **Dimensions:** `1024`
- **Metric:** `cosine`
- A supported serverless cloud and region

Create an API key with permission to query, upsert, and delete vectors in this
index. The Lambda functions need to reach Google Drive and Pinecone over the
internet. The template does not put them in a VPC; if you later add a VPC,
provide outbound internet access (for example, through a NAT gateway).

## 3. Enable the Bedrock embedding model

In the AWS region where the stack will run, open Amazon Bedrock and enable/access
the `amazon.titan-embed-text-v2:0` model as required by the account. The Lambda
execution roles created by SAM receive `bedrock:InvokeModel` permission for
this model.

## 4. Store application credentials in Secrets Manager

In **AWS Secrets Manager**, create a secret with type **Other type of secret**.
Its secret value must be one JSON object with these keys:

```json
{
  "google_service_account": {
    "type": "service_account",
    "project_id": "your-google-project",
    "private_key_id": "key-id",
    "private_key": "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n",
    "client_email": "service-account@your-google-project.iam.gserviceaccount.com",
    "client_id": "client-id",
    "token_uri": "https://oauth2.googleapis.com/token"
  },
  "pinecone_api_key": "your-pinecone-api-key"
}
```

Use the complete downloaded Google service-account JSON for the
`google_service_account` object. Save the secret and copy its ARN. Do not put
the credential values in GitHub Actions variables or commit them.

## 5. Create the GitHub Actions OIDC deployment role

In **IAM → Identity providers**, add an OpenID Connect provider:

- Provider URL: `https://token.actions.githubusercontent.com`
- Audience: `sts.amazonaws.com`

Create an IAM role for web identity using that provider. Restrict its trust
policy to this repository and the `main` branch (replace the placeholders):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::<account-id>:oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
          "token.actions.githubusercontent.com:sub": "repo:<owner>/<repository>:ref:refs/heads/main"
        }
      }
    }
  ]
}
```

Attach a deployment policy to this role. SAM/CloudFormation must be able to
manage the stack's resources and upload build artifacts. At minimum, scope the
policy to the target account and stack/resource names where possible and allow:

- CloudFormation stack and change-set create, describe, execute, update, and
  delete operations for this stack.
- S3 bucket/object operations for the SAM deployment artifact bucket.
- Lambda function, version, permission, and event-source-mapping operations.
- API Gateway REST API, deployment, stage, and method operations.
- SQS queue and DynamoDB table operations.
- IAM role and inline/managed policy creation, update, and deletion for the
  Lambda execution roles, plus `iam:PassRole` for those roles.

The permissions needed to provision CloudFormation resources are broad. Use a
dedicated deployment role, avoid long-lived access keys, limit the trust policy
to the exact repository/ref, and review the generated change set. For a
production account, further restrict resource ARNs and consider a CloudFormation
service role instead of granting resource provisioning permissions directly to
the GitHub-assumed role.

## 6. Configure repository variables

In GitHub, open **Settings → Secrets and variables → Actions → Variables** and
create these repository variables. Values are deployment configuration, not
credential contents.

| Variable | Value |
|---|---|
| `AWS_REGION` | AWS region for the stack and Bedrock model |
| `AWS_DEPLOY_ROLE_ARN` | ARN of the GitHub OIDC deployment role |
| `STACK_NAME` | For example, `bluecoins-rag-updater` |
| `CREDENTIALS_SECRET_ARN` | Secrets Manager ARN from step 4 |
| `GOOGLE_DRIVE_FILE_ID` | Drive ID from step 1 |
| `PINECONE_INDEX_NAME` | Existing Pinecone index name |

The ignored local `.env` file lists the same names for convenience; GitHub
Actions does not upload or read that file. Add the values separately as
repository variables. Keep only credential material in Secrets Manager.

## 7. Deploy the AWS application resources

Run **Actions → Deploy Bluecoins API → Run workflow**, selecting `main`, or
push a commit to `main`. The workflow:

1. Checks out the code and installs Python 3.12 and AWS SAM CLI.
2. Assumes the deployment role through GitHub OIDC.
3. Runs `sam build --parallel` on the hosted Linux runner, producing ZIP-based
   Python Lambda packages without Docker.
4. Runs `sam deploy --resolve-s3`, which creates/uses the SAM artifact bucket
   and deploys the CloudFormation stack.

The stack creates these AWS resources:

| Resource | Purpose |
|---|---|
| API Gateway REST API | IAM-authorized `/ingest`, `/ingest/{jobId}`, and `/search` routes |
| API Lambda | Enqueues ingestion, reports status, and searches Pinecone |
| Worker Lambda | Downloads Drive data, creates CSV, parses and indexes it |
| Lambda execution roles | DynamoDB/SQS/Secrets Manager/Bedrock access required by each function |
| SQS ingestion queue | Decouples long-running ingestion from API Gateway |
| SQS dead letter queue | Holds messages after three failed processing attempts |
| DynamoDB table | Stores ingestion job status and the active Pinecone namespace |
| SQS event source mapping | Invokes the worker for queued jobs |
| SAM artifact bucket | Stores deployment packages in S3; created/managed by SAM |

The stack outputs include the API base URL and dead-letter queue URL. The
Google service account, Pinecone index, Bedrock model access, and Secrets
Manager credential secret are prerequisites created separately.

## 8. Allow API callers and verify

Grant each API caller's IAM user/role `execute-api:Invoke` for the deployed API.
For example, replace the placeholders and scope this policy to the deployed
stage:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "execute-api:Invoke",
      "Resource": "arn:aws:execute-api:<region>:<account-id>:<api-id>/prod/*/*"
    }
  ]
}
```

Use an HTTP client that signs requests with AWS Signature Version 4 (service
name `execute-api`). After deployment:

1. `POST <ApiBaseUrl>/ingest` returns HTTP `202` and a job ID.
2. Poll `GET <ApiBaseUrl>/ingest/<job-id>` until status is `completed` or
   `failed`.
3. Query `GET <ApiBaseUrl>/search?q=monthly%20expenses&k=5`.

If a job fails, inspect its CloudWatch logs. Messages that exhaust retries
appear in the dead-letter queue; correct the cause before redriving them. A
successful refresh replaces the active Pinecone namespace after indexing,
then deletes the previous namespace.
