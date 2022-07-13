import subprocess
import time
import os
import boto3
import tempfile
from e2e.utils.prometheus.createIRSA_AMPIngest import (
#    setup_ingest_role,
#    delete_ingest_role,
    create_AMP_ingest_policy,
    delete_policy,
)
from e2e.utils.utils import get_aws_account_id
from e2e.utils.utils import wait_for
from e2e.fixtures.cluster import associate_iam_oidc_provider

prometheus_yaml_files_directory = "../../deployments/add-ons/prometheus"

def make_new_file_with_replacement(file_path, original_to_replacement_dict):
    new_file_contents = ""
    with open(file_path, 'r') as original_file:
        for original_line in original_file:
            new_line = original_line
            for key in original_to_replacement_dict:
                new_line = new_line.replace(key, original_to_replacement_dict.get(key))
            new_file_contents += new_line
    old_path_array = file_path.split('/')
    old_path_array[-1] = 'updated-' + old_path_array[-1]
    new_file_path = '/'.join(old_path_array)
    with open(new_file_path, 'w') as new_file:
        new_file.write(new_file_contents)
    return new_file_path

def update_AMP_service_account_id(AMP_service_account_file_path):
    aws_account_id = get_aws_account_id()
    original_to_replacement_dict = {}
    original_to_replacement_dict['<my-account-id>'] = aws_account_id
    return make_new_file_with_replacement(AMP_service_account_file_path, original_to_replacement_dict)
    
def update_config_map_AMP_workspace(workspace_id, region, config_map_file_path):
    original_to_replacement_dict = {}
    original_to_replacement_dict['<my-workspace-id>'] = workspace_id
    original_to_replacement_dict['<my-workspace-region>'] = region
    return make_new_file_with_replacement(config_map_file_path, original_to_replacement_dict)

def create_AMP_workspace(region):
    amp_client = boto3.client('amp', region_name=region)
    
    workspace_id = amp_client.create_workspace(alias = 'test_AMP_workspace')['workspaceId']
    print(f"Created Workspace ID: {workspace_id}")

    def check_active_workspace_status():
        workspace_status = amp_client.describe_workspace(workspaceId=workspace_id).get("workspace").get("status").get("statusCode")
        print(f"Workspace Status: {workspace_status}")
        assert "ACTIVE" == workspace_status
    
    wait_for(check_active_workspace_status)
    
    return workspace_id

def set_up_prometheus_for_AMP(cluster_name, region):
    workspace_id = create_AMP_workspace(region)
    
#    setup_ingest_role(cluster_name, region)

#    # Edit AMP service account to use account-id
#    new_AMP_service_account = update_AMP_service_account_id(f'{prometheus_yaml_files_directory}/AMP-service-account.yaml')

    print("About to associate IAM with OIDC provider.")
    associate_iam_oidc_provider(cluster_name, region)
    print("Finished associating IAM with OIDC provider.")

    # Create AMP service account:
    policy_arn = create_AMP_ingest_policy()
    create_AMP_service_account_command = f'eksctl create iamserviceaccount --name amp-iamproxy-ingest-service-account --role-name amp-iamproxy-ingest-role --namespace monitoring --cluster {cluster_name} --attach-policy-arn {policy_arn} --override-existing-serviceaccounts --approve --region {region}'.split()
    subprocess.call(create_AMP_service_account_command, encoding="utf-8")

    # Edit config map to use workspace-id and region
    new_config_map = update_config_map_AMP_workspace(workspace_id, region, f'{prometheus_yaml_files_directory}/config-map.yaml')
    
    create_namespace_command = 'kubectl create namespace monitoring'.split()
    subprocess.call(create_namespace_command)
    
    kustomize_build_command = f'kustomize build {prometheus_yaml_files_directory}'.split()
    print("About to run kustomize command.")
    kustomize_build_output = subprocess.check_output(kustomize_build_command)
    print("Finished running kustomize command.")
    
    print("About apply kustomize output.")
    with tempfile.NamedTemporaryFile() as tmp_file:
        tmp_file.write(kustomize_build_output)
        tmp_file.flush()
        kubectl_apply_command = f'kubectl apply -f {tmp_file.name}'.split()
        subprocess.call(kubectl_apply_command, encoding="utf-8")
    print("Finished applying kustomize output.")

    return workspace_id

def set_up_prometheus_port_forwarding():
    print("Entered prometheus port_forwarding_function.")
    def get_prometheus_pod_name():
        get_pod_name_command = 'kubectl get pods --namespace=monitoring'.split()
        pod_list = subprocess.check_output(get_pod_name_command, encoding="utf-8").split('\n')[1:]
        print(f"Monitoring Pods list: {pod_list}")
        assert 0 < len(pod_list)
        prometheus_pod_name = None
        for pod in pod_list:
            pod_name = pod.split()[0]
            print("pod name and status:", pod)
            pod_name_array = pod_name.split('-')
            if (('p' == pod_name[0])
                and ('prometheus' == pod_name_array[0])
                and ('deployment' == pod_name_array[1])):
                describe_pod_command = f'kubectl describe pod {pod_name} --namespace monitoring'.split()
                print('\n\nAbout to call describe pod command!\n\n')
                print(subprocess.check_output(describe_pod_command, encoding="utf-8"))
                print('\n\nDescribe Finished\n\n')
                get_ns_pod_logs_command = f'kubectl logs {pod_name} --namespace monitoring'.split()
                print("\n\nAbout to get monitoring namespace logs!\n\n")
                print(subprocess.check_output(get_ns_pod_logs_command, encoding="utf-8"))
                print('\n\nLogs Finished\n\n')
                pod_status = pod.split()[1]
                print("pod_status:", pod_status)
                assert "1/1" == pod_status
                print("Assert running was succesfull.")
                prometheus_pod_name = pod_name
                break
        return prometheus_pod_name

    prometheus_pod_name = wait_for(get_prometheus_pod_name, timeout=90, interval=30)
    if None == prometheus_pod_name:
        raise ValueError("Prometheus Pod Not Running.")

    set_up_port_forwarding_command = f'kubectl port-forward {prometheus_pod_name} 9090:9090 -n monitoring'.split()
    print(" ".join(set_up_port_forwarding_command))
    port_forwarding_process = subprocess.Popen(set_up_port_forwarding_command)
    time.sleep(10)  # Wait 10 seconds for port forwarding to open
    return port_forwarding_process
    
def check_prometheus_is_running():
    check_up_command = 'curl http://localhost:9090/api/v1/query?query=up'.split()
    print(' '.join(check_up_command))
    up_results = subprocess.check_output(check_up_command, encoding="utf-8")
    assert True==bool(up_results)

def get_kfp_create_experiment_count():
    prometheus_curl_command = "curl http://localhost:9090/api/v1/query?query=experiment_server_create_requests".split()
    prometheus_query_results = subprocess.check_output(prometheus_curl_command, encoding="utf-8")
    prometheus_create_experiment_count = prometheus_query_results.split(",")[-1].split('"')[1]
    print("prometheus_create_experiment_count:", prometheus_create_experiment_count)
    return prometheus_create_experiment_count
    
def check_AMP_connects_to_prometheus(region, workspace_id, expected_value):
    time.sleep(60) # Wait for prometheus to scrape.
    access_key = os.environ['AWS_ACCESS_KEY_ID']
    secret_key = os.environ['AWS_SECRET_ACCESS_KEY']

    print(f"Using Workspace ID: {workspace_id}")
    
    AMP_awscurl_command = f'awscurl --access_key {access_key} --secret_key {secret_key} --region {region} --service aps https://aps-workspaces.{region}.amazonaws.com/workspaces/{workspace_id}/api/v1/query?query=experiment_server_create_requests'.split()
    AMP_query_results = subprocess.check_output(AMP_awscurl_command, encoding="utf-8")
    AMP_create_experiment_count = AMP_query_results.split(",")[-1].split('"')[1]
    print("AMP_create_experiment_count:", AMP_create_experiment_count)

    prometheus_create_experiment_count = get_kfp_create_experiment_count()
    
    print(f"Asserting AMP == prometheus: {AMP_create_experiment_count} == {prometheus_create_experiment_count}")
    assert AMP_create_experiment_count == prometheus_create_experiment_count
    print(f"Asserting expected == prometheus: {expected_value} == {prometheus_create_experiment_count}")
    assert str(expected_value) == prometheus_create_experiment_count
    return AMP_create_experiment_count

def delete_AMP_resources(cluster_name, region, workspace_id):
#    print("About to delete ingest role.")
#    delete_ingest_role()
#    print("Finished deleting ingest role.")

    print("About to delete service account and role.")
    delete_AMP_service_account_command = f'eksctl delete iamserviceaccount --name amp-iamproxy-ingest-service-account --namespace monitoring --cluster {cluster_name} --region {region}'.split()
    subprocess.call(delete_AMP_service_account_command, encoding="utf-8")
    print("Finished deleting service account and role.")

    wait_for(delete_policy)
    
    amp_client = boto3.client('amp', region_name=region)

    print("About to delete workspace.")
    amp_client.delete_workspace(workspaceId=workspace_id)

    time.sleep(10) # Wait for the workspace to delete.
    
    try:
        amp_client.describe_workspace(workspaceId=workspace_id)
    except Exception as e:
        print(e)
        print("Finished deleting workspace.")
        return
    raise AssertionError("The workspace with id", workspace_id, "is in the {workspace_status} state.")
