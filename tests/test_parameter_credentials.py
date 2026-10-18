import json
import os
import unittest
from unittest.mock import patch

from lambda_src.lambda_function import _google_service_account, _pinecone_api_key


class ParameterCredentialsTests(unittest.TestCase):
    @patch("lambda_src.lambda_function.boto3.client")
    def test_google_credentials_are_read_from_the_google_parameter(self, mock_client) -> None:
        expected = {
            "client_email": "bluecoins@example.iam.gserviceaccount.com",
            "private_key": "private-key-placeholder",
        }
        mock_client.return_value.get_parameter.return_value = {
            "Parameter": {"Value": json.dumps(expected)}
        }

        with patch.dict(
            os.environ,
            {"GOOGLE_CREDENTIALS_PARAMETER_NAME": "/app/google-service-account"},
        ):
            self.assertEqual(_google_service_account(), expected)

        mock_client.assert_called_once_with("ssm")
        mock_client.return_value.get_parameter.assert_called_once_with(
            Name="/app/google-service-account",
            WithDecryption=True,
        )

    @patch("lambda_src.lambda_function.boto3.client")
    def test_pinecone_key_uses_its_separate_parameter(self, mock_client) -> None:
        mock_client.return_value.get_parameter.return_value = {
            "Parameter": {"Value": "pinecone-key-placeholder"}
        }

        with patch.dict(
            os.environ,
            {"PINECONE_API_KEY_PARAMETER_NAME": "/app/pinecone-api-key"},
        ):
            self.assertEqual(_pinecone_api_key(), "pinecone-key-placeholder")

        mock_client.return_value.get_parameter.assert_called_once_with(
            Name="/app/pinecone-api-key",
            WithDecryption=True,
        )


if __name__ == "__main__":
    unittest.main()
