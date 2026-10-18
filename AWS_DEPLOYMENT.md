# AWS and GitHub Actions setup

This guide provisions external credentials first, then lets the GitHub Actions
workflow deploy the AWS application stack. The workflow uses AWS SAM's native
Python ZIP build on Ubuntu and does not require Docker.

## 1. Prepare Google Drive access

1. In Google Cloud, create or select a project and enable the **Google Drive
   API**.
2. Create a service account and a JSON key. Copy its `client_email` and
   `private_key` into GitHub Actions secrets in step 4; do not commit or paste
   the key into chat.
3. Share the Bluecoins `.fydb` file in Drive with the service account's
   `client_email`, granting at least Viewer access.
4. Copy the file ID from its Drive URL. The ID is the value between `/d/` and
   `/edit` (or the `id` query parameter for some Drive URLs).

The workflow needs only the service account email and private key. Keep the
downloaded JSON key private and securely delete any local copy after setting up
the GitHub secret.

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

## 4. Add GitHub Actions secrets

In GitHub, open **Settings → Secrets and variables → Actions → Secrets** and
create these repository secrets:

| Secret | Value |
|---|---|
| `GOOGLE_SERVICE_ACCOUNT_EMAIL` | Service account `client_email` from step 1 |
| `GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY` | The complete PEM `private_key`, including begin/end lines |
| `PINECONE_API_KEY` | API key created in step 2 |

Paste the private key as a multiline secret. The workflow also accepts a
single-line key with literal `\n` separators. Never use repository variables
for these credentials, commit them, or paste them into chat.

Before deployment, GitHub Actions writes the Google email/key and Pinecone key
to separate ephemeral runner files and uploads them as AWS Systems Manager
Parameter Store **Standard-tier `SecureString`** parameters:
`/<STACK_NAME>/google-service-account` and `/<STACK_NAME>/pinecone-api-key`.
SSM encrypts both values with the AWS-managed `alias/aws/ssm` key. The temporary
runner files are removed at the end of the step. The API Lambda role can read
only the Pinecone parameter; the worker can read both.

Standard parameters have no additional Parameter Store storage charge and
support up to 4 KB per value. The workflow explicitly selects the Standard tier
and checks each value's size before writing, avoiding Advanced-tier parameter
charges. This avoids Secrets Manager's recurring secret-storage charge. AWS
service/KMS request charges, if applicable, are separate. Lambda reads the
values with `ssm:GetParameter` and narrowly scoped KMS decryption permissions;
the actual credentials are not placed in the Lambda environment or CloudFormation
parameters.

If you created the earlier Secrets Manager credential secret, first deploy
successfully and verify ingestion/search using these SSM parameters. Then
manually delete or schedule deletion of that now-unused secret in Secrets
Manager to stop its recurring storage charge. It is intentionally not deleted
automatically.

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
- `ssm:PutParameter` on `/<STACK_NAME>/google-service-account` and
  `/<STACK_NAME>/pinecone-api-key` for the workflow role so it can sync the
  GitHub secrets before deployment.
- `kms:Encrypt` for the AWS-managed `aws/ssm` key, constrained to SSM requests
  and the two parameter encryption contexts above. Standard SecureString writes
  use KMS encryption.
- IAM role and inline/managed policy creation, update, and deletion for the
  Lambda execution roles, plus `iam:PassRole` for those roles.

The permissions needed to provision CloudFormation resources are broad. Use a
dedicated deployment role, avoid long-lived access keys, limit the trust policy
to the exact repository/ref, and review the generated change set. For a
production account, further restrict resource ARNs and consider a CloudFormation
service role instead of granting resource provisioning permissions directly to
the GitHub-assumed role.

For the workflow role's KMS statement, use the same SSM-through-service and
parameter encryption-context restrictions as this example (replace placeholders):

```json
{
  "Effect": "Allow",
  "Action": "kms:Encrypt",
  "Resource": "*",
  "Condition": {
    "StringEquals": {
      "kms:ViaService": "ssm:<region>.amazonaws.com"
    },
    "ArnEquals": {
      "kms:EncryptionContext:PARAMETER_ARN": [
        "arn:aws:ssm:<region>:<account-id>:parameter/<stack-name>/google-service-account",
        "arn:aws:ssm:<region>:<account-id>:parameter/<stack-name>/pinecone-api-key"
      ]
    }
  }
}
```

## 6. Configure repository variables

In GitHub, open **Settings → Secrets and variables → Actions → Variables** and
create these repository variables. Values are deployment configuration, not
credential contents.

| Variable | Value |
|---|---|
| `AWS_REGION` | AWS region for the stack and Bedrock model |
| `AWS_DEPLOY_ROLE_ARN` | ARN of the GitHub OIDC deployment role |
| `STACK_NAME` | For example, `bluecoins-rag-updater` |
| `GOOGLE_DRIVE_FILE_ID` | Drive ID from step 1 |
| `PINECONE_INDEX_NAME` | Existing Pinecone index name |

The ignored local `.env` file is only a local reference and is not uploaded or
read by the workflow. Add deployment settings as repository variables and add
credential values separately as the repository secrets from step 4.

## 7. Deploy the AWS application resources

Run **Actions → Deploy Bluecoins API → Run workflow**, selecting `main`, or
push a commit to `main`. The workflow:

1. Checks out the code and installs Python 3.12 and AWS SAM CLI.
2. Assumes the deployment role through GitHub OIDC.
3. Writes the GitHub credential secrets to two Standard `SecureString`
   parameters in Parameter Store (overwriting prior values when present).
4. Runs `sam build --parallel` on the hosted Linux runner, producing ZIP-based
   Python Lambda packages without Docker.
5. Runs `sam deploy --resolve-s3`, which creates/uses the SAM artifact bucket
   and deploys the CloudFormation stack.

The stack creates these AWS resources:

| Resource | Purpose |
|---|---|
| API Gateway REST API | IAM-authorized `/ingest`, `/ingest/{jobId}`, and `/search` routes |
| API Lambda | Enqueues ingestion, reports status, and searches Pinecone |
| Worker Lambda | Downloads Drive data, creates CSV, parses and indexes it |
| Lambda execution roles | DynamoDB/SQS/Bedrock plus least-privilege SSM reads and KMS decrypt permissions |
| SQS ingestion queue | Decouples long-running ingestion from API Gateway |
| SQS dead letter queue | Holds messages after three failed processing attempts |
| DynamoDB table | Stores ingestion job status and the active Pinecone namespace |
| SQS event source mapping | Invokes the worker for queued jobs |
| SSM SecureString parameters | Store GitHub-provided Google and Pinecone credentials outside Lambda configuration |
| SAM artifact bucket | Stores deployment packages in S3; created/managed by SAM |

The stack outputs include the API base URL and dead-letter queue URL. The
GitHub repository secrets, Google service account, Pinecone index, and Bedrock
model access are prerequisites. The deployment workflow creates/updates the
standard SSM parameter.

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

The two SSM parameters are managed by the deployment workflow rather than
CloudFormation. If you permanently remove the application, delete them
separately after confirming they are no longer needed:

```bash
aws ssm delete-parameter --name "/<STACK_NAME>/google-service-account"
aws ssm delete-parameter --name "/<STACK_NAME>/pinecone-api-key"
```
