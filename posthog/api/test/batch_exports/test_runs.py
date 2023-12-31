from django.test.client import Client as HttpClient
import pytest

from rest_framework import status
from posthog.api.test.batch_exports.conftest import start_test_worker
from posthog.api.test.batch_exports.operations import (
    backfill_batch_export_ok,
    create_batch_export_ok,
    get_batch_export,
    get_batch_export_runs,
    get_batch_export_runs_ok,
)
from posthog.api.test.test_organization import create_organization
from posthog.api.test.test_team import create_team
from posthog.api.test.test_user import create_user


from posthog.temporal.client import sync_connect

pytestmark = [
    pytest.mark.django_db,
]


def test_can_get_export_runs_for_your_organizations(client: HttpClient):
    temporal = sync_connect()

    destination_data = {
        "type": "S3",
        "config": {
            "bucket_name": "my-production-s3-bucket",
            "region": "us-east-1",
            "prefix": "posthog-events/",
            "batch_window_size": 3600,
            "aws_access_key_id": "abc123",
            "aws_secret_access_key": "secret",
        },
    }

    batch_export_data = {
        "name": "my-production-s3-bucket-destination",
        "destination": destination_data,
        "interval": "hour",
    }

    organization = create_organization("Test Org")
    team = create_team(organization)
    user = create_user("test@user.com", "Test User", organization)
    client.force_login(user)

    with start_test_worker(temporal):
        response = create_batch_export_ok(
            client,
            team.pk,
            batch_export_data,
        )

        response = get_batch_export_runs(client, team.pk, response["id"])
        assert response.status_code == status.HTTP_200_OK, response.json()


def test_cannot_get_exports_for_other_organizations(client: HttpClient):
    temporal = sync_connect()

    destination_data = {
        "type": "S3",
        "config": {
            "bucket_name": "my-production-s3-bucket",
            "region": "us-east-1",
            "prefix": "posthog-events/",
            "batch_window_size": 3600,
            "aws_access_key_id": "abc123",
            "aws_secret_access_key": "secret",
        },
    }

    batch_export_data = {
        "name": "my-production-s3-bucket-destination",
        "destination": destination_data,
        "interval": "hour",
    }

    organization = create_organization("Test Org")
    team = create_team(organization)
    user = create_user("test@user.com", "Test User", organization)

    another_organization = create_organization("Another Org")
    another_user = create_user("another-test@user.com", "Another Test User", another_organization)

    with start_test_worker(temporal):
        client.force_login(user)
        response = create_batch_export_ok(
            client,
            team.pk,
            batch_export_data,
        )

        client.force_login(another_user)
        response = get_batch_export_runs(client, team.pk, response["id"])
        assert response.status_code == status.HTTP_403_FORBIDDEN, response.json()


def test_batch_exports_are_partitioned_by_team(client: HttpClient):
    """
    You shouldn't be able to fetch a BatchExport by id, via a team that it
    doesn't belong to.
    """
    temporal = sync_connect()

    destination_data = {
        "type": "S3",
        "config": {
            "bucket_name": "my-production-s3-bucket",
            "region": "us-east-1",
            "prefix": "posthog-events/",
            "batch_window_size": 3600,
            "aws_access_key_id": "abc123",
            "aws_secret_access_key": "secret",
        },
    }

    batch_export_data = {
        "name": "my-production-s3-bucket-destination",
        "destination": destination_data,
        "interval": "hour",
    }

    organization = create_organization("Test Org")
    team = create_team(organization)
    another_team = create_team(organization)
    user = create_user("test@user.com", "Test User", organization)

    with start_test_worker(temporal):
        client.force_login(user)
        batch_export = create_batch_export_ok(
            client,
            team.pk,
            batch_export_data,
        )

        response = get_batch_export(client, another_team.pk, batch_export["id"])
        assert response.status_code == status.HTTP_404_NOT_FOUND, response.json()

        # And switch the teams around for good measure.
        batch_export = create_batch_export_ok(
            client,
            another_team.pk,
            batch_export_data,
        )

        response = get_batch_export(client, team.pk, batch_export["id"])
        assert response.status_code == status.HTTP_404_NOT_FOUND, response.json()


def test_batch_export_backfill_creates_a_run(client: HttpClient):
    temporal = sync_connect()

    destination_data = {
        "type": "S3",
        "config": {
            "bucket_name": "my-production-s3-bucket",
            "region": "us-east-1",
            "prefix": "posthog-events/",
            "batch_window_size": 3600,
            "aws_access_key_id": "abc123",
            "aws_secret_access_key": "secret",
        },
    }

    batch_export_data = {
        "name": "my-production-s3-bucket-destination",
        "destination": destination_data,
        "interval": "hour",
    }

    organization = create_organization("Test Org")
    team = create_team(organization)
    user = create_user("test@user.com", "Test User", organization)
    client.force_login(user)

    with start_test_worker(temporal):
        batch_export = create_batch_export_ok(
            client,
            team.pk,
            batch_export_data,
        )

        runs = get_batch_export_runs_ok(client, team.pk, batch_export["id"])

        assert len(runs["results"]) == 0

        backfill_batch_export_ok(client, team.pk, batch_export["id"], "2021-01-01T00:00:00", "2021-01-01T01:00:00")

        runs = get_batch_export_runs_ok(client, team.pk, batch_export["id"])

        assert len(runs["results"]) == 1

        run = runs["results"][0]
        assert run["data_interval_start"] == "2021-01-01T00:00:00Z"
        assert run["data_interval_end"] == "2021-01-01T01:00:00Z"
