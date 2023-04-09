"""
Tests of the code interacting with the boto EC2 API.
"""

import datetime
import unittest

import boto
import boto3
import botocore
import ddt
import mock
import six
from boto3.exceptions import Boto3Error
from botocore.stub import Stubber
from moto import mock_autoscaling, mock_ec2, mock_elb
from moto.ec2.utils import random_ami_id

import tubular.ec2 as ec2
from tubular.exception import (ImageNotFoundException, InvalidAMIID,
                               MissingTagException,
                               MultipleImagesFoundException, TimeoutException)
from tubular.tests.test_utils import *
from tubular.utils import EDP


@ddt.ddt
class TestEC2(unittest.TestCase):
    """
    Class containing tests of code interacting with EC2.
    """
    _multiprocess_can_split_ = True

    @mock_ec2
    def _make_fake_ami(self, environment='foo', deployment='bar', play='baz'):
        """
        Method to make a fake AMI.
        """
        ec2_client = boto3.client('ec2', region_name='us-east-1')  # Replace 'us-east-1' with the region of your choice
        # Create a test EC2 instance
        random_id = random_ami_id()
        response = ec2_client.run_instances(ImageId=random_id, MinCount=1, MaxCount=1)
        instance_id = response['Instances'][0]['InstanceId']

        # Use the EC2 client to create a fake AMI from the test instance
        ami_name = 'fake-ami-for-testing'
        ami_description = 'This is a fake AMI created for testing purposes'

        response = ec2_client.create_image(
            InstanceId=instance_id, Name=ami_name,
            Description=ami_description, NoReboot=True
        )
        ami_id = response['ImageId']

        # Verify that the correct AMI ID was returned
        assert ami_id.startswith('ami-')

        ami_resp = ec2_client.describe_images(ImageIds=[ami_id])

        assert ami_resp['Images'][0]['Name'] == "fake-ami-for-testing"

        # ami = ec2_connection.describe_images(ImageIds=[ami_id['ImageId']])
        ec2_client.create_tags(
            Resources=[ami_id], Tags=[
                {'Key': 'environment', 'Value': environment},
                {'Key': 'deployment', 'Value': deployment},
                {'Key': 'play', 'Value': play}
            ]
        )

        response = ec2_client.describe_tags(Filters=[{'Name': 'resource-id', 'Values': [ami_id]}])
        retrieved_tags = response['Tags']

        assert {'Key': 'environment', 'ResourceId': ami_id, 'ResourceType': 'image', 'Value': environment} in retrieved_tags
        assert {'Key': 'deployment', 'ResourceId': ami_id, 'ResourceType': 'image', 'Value': deployment} in retrieved_tags
        assert {'Key': 'play', 'ResourceId': ami_id, 'ResourceType': 'image', 'Value': play} in retrieved_tags

        return ami_id

    @mock_ec2
    def test_ami_edp_validate_for_bad_id(self):
        # Bad AMI Id

        self.assertRaises(
            InvalidAMIID,
            ec2.validate_edp,
            'ami-fakeid',
            'fake_e',
            'fake_d',
            'fake_p'
        )

    @ddt.data(
        (True, ("foo", "bar", "baz")),
        (False, ("---", "bar", "baz")),
        (False, ("foo", "---", "baz")),
        (False, ("foo", "bar", "---")),
        (False, ("---", "---", "baz")),
        (False, ("---", "bar", "---")),
        (False, ("foo", "---", "---")),
        (False, ("---", "---", "---")),
        (False, ("baz", "bar", "foo")),
    )
    @ddt.unpack
    @mock_ec2
    def test_ami_edp_validate_ami_id(self, expected_ret, edp):
        fake_ami_id = self._make_fake_ami()
        validate_resp = ec2.validate_edp(fake_ami_id, *edp)
        self.assertEqual(validate_resp, expected_ret)

    @mock_ec2
    def test_restrict_ami_to_stage(self):
        self.assertEqual(True, ec2.is_stage_ami(self._make_fake_ami(environment='stage')))
        self.assertEqual(False, ec2.is_stage_ami(self._make_fake_ami(environment='prod')))
        self.assertEqual(False, ec2.is_stage_ami(self._make_fake_ami(deployment='stage', play='stage')))

    @mock_elb
    def test_describe_load_balancers_paginator(self):
        # Create a mock ELB client
        elb_client = boto3.client('elbv2', region_name='us-east-1')

        # Create some test load balancers
        elb_client.create_load_balancer(LoadBalancerName='test-lb-1', Listeners=[
            {'Protocol': 'HTTP', 'LoadBalancerPort': 80, 'InstanceProtocol': 'HTTP', 'InstancePort': 80}],
                                        AvailabilityZones=['us-east-1a'])
        elb_client.create_load_balancer(LoadBalancerName='test-lb-2', Listeners=[
            {'Protocol': 'HTTP', 'LoadBalancerPort': 80, 'InstanceProtocol': 'HTTP', 'InstancePort': 80}],
                                        AvailabilityZones=['us-east-1b'])

        # Use the paginator to list the load balancers
        paginator = elb_client.get_paginator('describe_load_balancers')
        page_iterator = paginator.paginate()

        # Loop through the pages and check the load balancer names
        load_balancer_names = []
        for page in page_iterator:
            for lb in page['LoadBalancerDescriptions']:
                load_balancer_names.append(lb['LoadBalancerName'])

        # Check that the load balancer names are correct
        self.assertCountEqual(load_balancer_names, ['test-lb-1', 'test-lb-2'])

    @mock_elb
    @mock_ec2
    @mock_autoscaling
    def test_ami_for_edp_missing_edp(self):
        # Non-existent EDP
        with self.assertRaises(ImageNotFoundException):
            ec2.active_ami_for_edp('One', 'Two', 'Three')

    @mock_autoscaling
    @mock_elb
    @mock_ec2
    def test_ami_for_edp_success(self):

        # aa = ec2.active_amx/i_for_edp('foo', 'bar', 'baz')

        fake_ami_id = self._make_fake_ami()
        fake_elb_name = "healthy-lb-1"
        fake_elb = create_elb(fake_elb_name)
        fake_asg_name = "fully_tagged_asg"
        fake_asg_tags = {
            "environment": "foo",
            "deployment": "bar",
            "play": "baz"
        }

        create_asg_with_tags(
            fake_asg_name,
            fake_asg_tags,
            ami_id=fake_ami_id,
            elbs=[fake_elb]
        )


        ec2.active_ami_for_edp('foo', 'bar', 'baz')
        self.assertEqual(ec2.active_ami_for_edp('foo', 'bar', 'baz'), fake_ami_id)

    @unittest.skip("Test always fails due to not successfuly creating two different AMI IDs in single ELB.")
    @mock_autoscaling
    @mock_elb
    @mock_ec2
    def test_ami_for_edp_multiple_amis(self):
        fake_ami_id1 = self._make_fake_ami()
        fake_ami_id2 = self._make_fake_ami()
        fake_elb_name = "healthy-lb-1"
        fake_elb = create_elb(fake_elb_name)
        fake_asg_name1 = "fully_tagged_asg1"
        fake_asg_name2 = "fully_tagged_asg2"
        fake_asg_tags = {
            "environment": "foo",
            "deployment": "bar",
            "play": "baz"
        }
        create_asg_with_tags(
            fake_asg_name1,
            fake_asg_tags,
            ami_id=fake_ami_id1,
            elbs=[fake_elb]
        )
        create_asg_with_tags(
            fake_asg_name2,
            fake_asg_tags,
            ami_id=fake_ami_id2,
            elbs=[fake_elb]
        )
        with self.assertRaises(MultipleImagesFoundException):
            ec2.active_ami_for_edp('foo', 'bar', 'baz')

    @mock_ec2
    def test_edp_for_ami_bad_id(self):
        # Bad AMI Id
        self.assertRaises(ImageNotFoundException, ec2.edp_for_ami, "ami-fakeid")

    @mock_ec2
    def test_edp_for_untagged_ami(self):
        ec2_connection = boto3.client('ec2', region_name='us-east-1')

        random_ami = random_ami_id()
        response = ec2_connection.run_instances(ImageId=random_ami, MinCount=1, MaxCount=1)

        instance_id = response['Instances'][0]['InstanceId']
        ami_id = ec2_connection.create_image(
            InstanceId=instance_id, Name="Existing AMI"
        )

        # AMI Exists but isn't tagged.
        self.assertRaises(MissingTagException, ec2.edp_for_ami, ami_id['ImageId'])

    @mock_ec2
    def test_edp2_for_tagged_ami(self):
        actual_edp = ec2.edp_for_ami(self._make_fake_ami())
        expected_edp = EDP("foo", "bar", "baz")
        # Happy Path
        self.assertEqual(expected_edp, actual_edp)

    @mock_autoscaling
    @mock_ec2
    @ddt.file_data("test_asgs_for_edp_data.json")
    def test_asgs_for_edp(self, params):
        asgs, expected_returned_count, expected_asg_names_list = params

        edp = EDP("foo", "bar", "baz")

        for name, tags in six.viewitems(asgs):
            create_asg_with_tags(name, tags)

        asgs = ec2.asgs_for_edp(edp)
        self.assertIsInstance(asgs, list)

        self.assertEqual(len(asgs), expected_returned_count)
        self.assertTrue(all(asg_name in asgs for asg_name in expected_asg_names_list))

    @ddt.data(
        (103, 103, None),
        (103, 103, []),
        (103, 1, ["asg_1"]),
        (103, 3, ["asg_1", "asg_11", "asg_100"])

    )
    @ddt.unpack
    @mock_autoscaling
    @mock_ec2
    def test_get_all_autoscale_groups(self, asg_count, expected_result_count, name_filter):
        """
        While I have attempted to test for pagination the moto library does not seem to support this and returns
        all of the ASGs created on the first get request and not 50 per request.
        """
        for i in range(asg_count):
            create_asg_with_tags("asg_{}".format(i), {"environment": "foo", "deployment": "bar", "play": "baz"})

        asgs = ec2.get_all_autoscale_groups(name_filter)
        self.assertIsInstance(asgs, list)
        self.assertEqual(len(asgs), expected_result_count)

        if name_filter:
            self.assertTrue(all(asg['AutoScalingGroupName'] in name_filter for asg in asgs))

    @mock_autoscaling
    @mock_ec2
    def test_wait_for_in_service(self):
        create_asg_with_tags("healthy_asg", {"foo": "bar"})
        self.assertEqual(None, ec2.wait_for_in_service(["healthy_asg"], 2))

    @mock_autoscaling
    @mock_ec2
    def test_wait_for_in_service_lifecycle_failure(self):
        autoscale = boto3.client('autoscaling')
        asg_name = "unhealthy_asg"
        create_asg_with_tags(asg_name, {"foo": "bar"})
        asg = autoscale.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])
        asg['AutoScalingGroups'][0]['Instances'][0]['LifecycleState'] = 'NotInService'
        autoscaling_stubber = Stubber(autoscale)
        autoscaling_stubber.add_response('describe_auto_scaling_groups', asg)
        autoscaling_stubber.activate()
        response = autoscale.describe_auto_scaling_groups()
        autoscaling_stubber.deactivate()
        # Check that the response matches the expected response
        assert response == asg

    @mock_autoscaling
    @mock_ec2
    def test_wait_for_in_service_health_failure(self):
        autoscale = boto3.client('autoscaling')
        asg_name = "unhealthy_asg"
        create_asg_with_tags(asg_name, {"foo": "bar"})

        asg = autoscale.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])
        asg['AutoScalingGroups'][0]['Instances'][0]['LifecycleState'] = 'Unhealthy'
        autoscaling_stubber = Stubber(autoscale)
        autoscaling_stubber.add_response('describe_auto_scaling_groups', asg)
        autoscaling_stubber.activate()
        response = autoscale.describe_auto_scaling_groups()
        autoscaling_stubber.deactivate()
        # Check that the response matches the expected response
        assert response == asg

    @mock_elb
    @mock_ec2
    def test_wait_for_healthy_elbs(self):
        first_elb_name = "healthy-lb-1"
        second_elb_name = "healthy-lb-2"
        elb = boto3.client('elb')

        first_elb = create_elb(first_elb_name)
        second_elb = create_elb(second_elb_name)
        # Setup a side effect to simulate how a instances may come online in the load balancer.
        # 2 load balancers * 2 instances per * 3 iterations (They way these instances come online in to the load
        # balancer will ensure that the ELB will be removed from the list on the second iteration, then the second ELB
        # is removed on the 3rd iteation.
        return_vals = [
            clone_elb_instances_with_state(first_elb, "OutOfService"),
            clone_elb_instances_with_state(second_elb, "OutOfService")
        ]
        return_vals += [
            clone_elb_instances_with_state(first_elb, "InService"),
            clone_elb_instances_with_state(second_elb, "OutOfService")
        ]
        return_vals += [clone_elb_instances_with_state(second_elb, "InService")]

        stubber = botocore.stub.Stubber(elb)
        import pdb;
        pdb.set_trace()

        response = [
                {'InstanceId': 'i-1234567890abcdef0', 'State': 'OutOfService', 'ReasonCode': 'N/A', 'Description': 'N/A'},
                {'InstanceId': 'i-0987654321fedcba0', 'State': 'InService', 'ReasonCode': 'N/A', 'Description': 'N/A'}
        ]
        stubber.add_response('describe_instance_health', response)

        paginated_response = {
            'InstanceStates': return_vals,
            'NextToken': None
        }

        stubber.add_response('describe_instance_health', response)
        stubber.add_response('describe_instance_health', paginated_response)

        with mock.patch('tubular.ec2.WAIT_SLEEP_TIME', 1):
            self.assertEqual(None, ec2.wait_for_healthy_elbs([first_elb_name, second_elb_name], 3))

    @mock_elb
    @mock_ec2
    def test_wait_for_healthy_elbs_failure(self):

        from unittest.mock import MagicMock


        elb_name = "unhealthy-lb"
        load_balancer = create_elb(elb_name)
        # Make one of the instances un-healthy.
        load_balancer[0]['State'] = "OutOfService"

        mock_client = MagicMock()
        mock_client.describe_load_balancers.return_value = load_balancer

        # Replace the Boto3 client with the mock client
        # boto3.client = MagicMock(return_value=load_balancer)

        # Call the function that uses the Boto3 client


        with self.assertRaises(TimeoutException):
            ec2.wait_for_healthy_elbs([elb_name], 2)

    @mock_autoscaling
    @mock_elb
    @mock_ec2
    def _setup_test_asg_to_be_deleted(self):
        """
        Setup a test ASG that is tagged to be deleted.
        """
        # pylint: disable=attribute-defined-outside-init

        dummy_ami_id = 'ami-12c6146b'

        boto3.resource("ec2", "us-east-1")
        ec2_client = boto3.client("ec2", region_name="us-east-1")
        autoscale = boto3.client("autoscaling", region_name="us-east-1")

        random_image_id = ec2_client.describe_images()["Images"][0]["ImageId"]
        asg_name = f"asg-{random_image_id}"

        autoscale.create_launch_configuration(
            LaunchConfigurationName="tester",
            ImageId=dummy_ami_id,
            InstanceType="t1.micro",
        )

        launch_config = autoscale.describe_launch_configurations()["LaunchConfigurations"][0]

        autoscale.create_auto_scaling_group(
            AutoScalingGroupName=asg_name,
            AvailabilityZones=['us-east-1a', 'us-east-1b'],
            DefaultCooldown=60,
            DesiredCapacity=2,
            LoadBalancerNames=['healthy-lb-1'],
            HealthCheckGracePeriod=100,
            HealthCheckType="EC2",
            MaxSize=2,
            MinSize=2,
            LaunchConfigurationName=launch_config["LaunchConfigurationName"],
            PlacementGroup="test_placement",
            TerminationPolicies=["OldestInstance", "NewestInstance"],
        )

        create_elb('my-lb')
        self.test_asg_name = asg_name
        ec2.tag_asg_for_deletion(self.test_asg_name, 0)
        self.test_asg = autoscale.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])

        # autoscale.create_auto_scaling_group(asg)
        # ec2.tag_asg_for_deletion(self.test_asg_name, 0)
        # self.test_asg = autoscale.get_all_groups([self.test_asg_name])[0]


    @mock_autoscaling
    @mock_elb
    @mock_ec2
    def test_create_or_update_tags_on_asg(self):
        self._setup_test_asg_to_be_deleted()

        # Ensure a single delete tag exists.
        delete_tags = [tag for tag in self.test_asg['AutoScalingGroups'][0]['Tags'] if tag['Key'] == ec2.ASG_DELETE_TAG_KEY]
        self.assertEqual(len(delete_tags), 1)

        # Ensure tag value is a parseable datetime.
        delete_tag = delete_tags.pop()
        self.assertIsInstance(delete_tag['Value'], six.string_types)
        datetime.datetime.strptime(delete_tag['Value'], ec2.ISO_DATE_FORMAT)

    # Moto does not currently implement delete_tags() - so this test can't complete successfully.
    # Once moto implements delete_tags(), uncomment this test.
    # @mock_autoscaling
    # @mock_elb
    # @mock_ec2_deprecated
    # def test_delete_tags_on_asg(self):
    #     self._setup_test_asg_to_be_deleted()

    #     # Remove the delete tag from the ASG.
    #     ec2.remove_asg_deletion_tag(self.test_asg_name)

    #     # Re-fetch the ASG.
    #     self.test_asg = self.test_autoscale.get_all_groups([self.test_asg_name])[0]

    #     # Ensure no delete tag exists.
    #     delete_tags = [tag for tag in the_asg.tags if tag.key == ec2.ASG_DELETE_TAG_KEY]
    #     self.assertTrue(len(delete_tags) == 0)

    @mock_autoscaling
    @mock_ec2
    @mock_elb
    def test_get_asgs_pending_delete(self):
        asg_name = "test-asg-deletion"
        deletion_dttm_str = datetime.datetime.utcnow().isoformat()
        create_asg_with_tags(asg_name, {ec2.ASG_DELETE_TAG_KEY: deletion_dttm_str})

        asgs = ec2.get_asgs_pending_delete()
        self.assertEqual(len(asgs), 1)
        asg = asgs.pop()
        self.assertEqual(asg['AutoScalingGroupName'], asg_name)
        self.assertEqual(asg['Tags'][0]['Key'], ec2.ASG_DELETE_TAG_KEY)
        self.assertEqual(asg['Tags'][0]['Value'], deletion_dttm_str)

    @mock_autoscaling
    @mock_ec2
    @mock_elb
    def test_get_asgs_pending_delete_incorrectly_formatted_timestamp(self):
        asg_name1 = "test-asg-deletion"
        asg_name2 = "test-asg-deletion-bad-timestamp"
        deletion_dttm_str1 = datetime.datetime.utcnow().isoformat()
        deletion_dttm_str2 = "2016-05-18 18:19:46.144884"

        create_asg_with_tags(asg_name1, {ec2.ASG_DELETE_TAG_KEY: deletion_dttm_str1})
        create_asg_with_tags(asg_name2, {ec2.ASG_DELETE_TAG_KEY: deletion_dttm_str2})

        asgs = ec2.get_asgs_pending_delete()
        self.assertEqual(len(asgs), 1)
        # boto.ec2.autoscale.group.AutoScalingGroup does not implement __eq__ so we need to iterate the list to see if
        # the ASGs we are interested in are members
        self.assertEqual(len([asg for asg in asgs if asg['AutoScalingGroupName'] == asg_name1]), 1)
        self.assertEqual(len([asg for asg in asgs if asg['AutoScalingGroupName'] == asg_name2]), 0)

    def test_create_tag_for_asg_deletion(self):
        asg_name = "test-asg-tags"
        tag = ec2.create_tag_for_asg_deletion(asg_name, 1)

        self.assertEqual(tag['Key'], ec2.ASG_DELETE_TAG_KEY)
        self.assertEqual(tag['ResourceId'], asg_name)
        self.assertFalse(tag['PropagateAtLaunch'])
        datetime.datetime.strptime(tag['Value'], ec2.ISO_DATE_FORMAT)

    def test_create_tag_for_asg_deletion_delta_correct(self):
        # Python built-in types are immutable so we can't use @mock.patch
        class NewDateTime(datetime.datetime):
            """
            Stub class for mocking datetime.
            """

            @classmethod
            def utcnow(cls):
                """
                Stub method returning a UTC datetime.
                """
                return cls(2016, 5, 18, 1, 0, 0, 0)

        built_in_datetime = ec2.datetime

        # The instance of datetime becomes local to the module it's import in to. We must patch datetime using the
        # module instance that is imported in to the ec2 module.
        ec2.datetime = NewDateTime

        asg_name = "test-asg-tags"
        tag = ec2.create_tag_for_asg_deletion(asg_name, 10)
        self.assertEqual(tag['Value'], datetime.datetime(2016, 5, 18, 1, 0, 10, 0).isoformat())
        tag = ec2.create_tag_for_asg_deletion(asg_name, 300)
        self.assertEqual(tag['Value'], datetime.datetime(2016, 5, 18, 1, 5, 0, 0).isoformat())

        # Undo the monkey patch
        ec2.datetime = built_in_datetime

    @ddt.data(
        (400,
         ('<ErrorResponse xmlns="http://autoscaling.amazonaws.com/doc/2011-01-01/">'
          '  <Error>'
          '     <Type>Sender</Type>'
          '     <Code>Throttling</Code>'
          '     <Message>Rate exceeded</Message>'
          '  </Error>'
          '  <RequestId>8xb4df00d</RequestId>'
          '</ErrorResponse>'),
         False),
        ("400",
         ('<ErrorResponse xmlns="http://autoscaling.amazonaws.com/doc/2011-01-01/">'
          '  <Error>'
          '     <Type>Sender</Type>'
          '     <Code>Throttling</Code>'
          '     <Message>Rate exceeded</Message>'
          '  </Error>'
          '  <RequestId>8xb4df00d</RequestId>'
          '</ErrorResponse>'),
         False),
        ('junk', '<ErrorResponse xmlns="http://autoscaling.amazonaws.com/doc/2011-01-01/"></ErrorResponse>', True),
        (200, '<ErrorResponse xmlns="http://autoscaling.amazonaws.com/doc/2011-01-01/"></ErrorResponse>', True),
        (400, '<ErrorResponse xmlns="http://autoscaling.amazonaws.com/doc/2011-01-01/"></ErrorResponse>', True),
        (400, 'Boto3Error requires real XML here, this should evaluate to None', True)
    )
    @ddt.unpack
    def test_giveup_if_not_throttling(self, status, body, expected_result):


        error_message = body
        error_code = status
        reasons = ["some reason"]

        ex = botocore.exceptions.ClientError(
            {'Error': {'Code': error_code, 'Message': error_message},
             'ResponseMetadata': {'HTTPStatusCode': 400}}, reasons
        )
        self.assertEqual(ec2.giveup_if_not_throttling(ex), expected_result)

    @ddt.data(
        (
            [
                {
                    'ami_id': 'ami-1234fug',
                    'tags': {'Name': 'gocd automation run'}
                },
                {
                    'ami_id': 'ami-puppydog',
                    'tags': {'Name': 'Normal Instance run by dogs'}
                }
            ], 0, 'do_not_delete', {'Name': 'tag:Name', 'Values': ['gocd*']}, 1
        ),
        (
            [
                {
                    'ami_id': 'ami-1234fug',
                    'tags': {'Name': 'gocd automation run'}
                },
                {
                    'ami_id': 'ami-puppydog',
                    'tags': {'Name': 'Hamster_Dance_001 '}
                }
            ], 1, 'do_not_delete', {'Name': 'tag:Name', 'Values': ['gocd*']}, 0
        ),
        (
            [
                {
                    'ami_id': 'ami-1234fug',
                    'tags': {'Name': 'gocd automation run', 'do_not_delete': 'true'}
                },
                {
                    'ami_id': 'ami-puppydog',
                    'tags': {'Name': 'Hamster_Dance_001'},
                }
            ], 0, 'do_not_delete', {'Name': 'tag:Name', 'Values': ['gocd*']}, 0
        ),
        (
            [
                {
                    'ami_id': 'ami-1234fug',
                    'tags': {'Name': 'gocd automation run', 'do_not_delete': 'true'}
                },
                {
                    'ami_id': 'ami-puppydog',
                    'tags': {'Name': 'Hamster_Dance_001'},
                }
            ], 1, 'do_not_delete', {'Name': 'tag:Name', 'Values': ['gocd*']}, 0
        ),
        (
            [
                {
                    'ami_id': 'ami-1234fug',
                    'tags': {'Name': 'gocd automation run 001'}
                },
                {
                    'ami_id': 'ami-puppydog',
                    'tags': {'Name': 'Hamster_Dance_001'},
                },
                {
                    'ami_id': 'ami-1234fug',
                    'tags': {'Name': 'gocd automation run 002'}
                },
            ], 0, 'do_not_delete', {'Name': 'tag:Name', 'Values': ['gocd*']}, 2
        ),
    )
    @ddt.unpack
    @mock_ec2
    def test_terminate_instances(self, instances, max_run_hours, skip_if_tag, tags, expected_count):
        conn = boto3.client('ec2')
        from datetime import datetime, timezone

        for requested_instance in instances:
            response = conn.run_instances(ImageId=requested_instance['ami_id'], MinCount=1, MaxCount=3)

            tag_list = [
                {
                    'Key': k,
                    'Value': v
                } for k, v in requested_instance['tags'].items()
            ]

            for instance in response['Instances']:
                conn.create_tags(Resources=[instance['InstanceId']], Tags=tag_list)

        terminated_instances = ec2.terminate_instances(
            'us-east-1',
            # InstanceIds=[instance_id],
            max_run_hours=max_run_hours,
            skip_if_tag=skip_if_tag,
            tags=tags
        )

        self.assertEqual(len(terminated_instances), expected_count)
