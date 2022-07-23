import subprocess
import time
import re
import os
import boto3
import tempfile
from e2e.utils.prometheus.createPolicy_AMPIngest import *
from e2e.utils.utils import wait_for
from e2e.fixtures.cluster import associate_iam_oidc_provider

prometheus_yaml_files_directory = "../../deployments/add-ons/prometheus"
default_prometheus_query = 'experiment_server_create_requests'
local_prometheus_port_forwarding_port = "9090"

def replace_params_env_in_line(file_path, original_to_replacement_dict):
    updated_file_contents = ""
    with open(file_path, 'r') as original_file:
        for line in original_file:
            for key in original_to_replacement_dict:
                if key == line.split("=")[0]:
                    line = f'{key}={original_to_replacement_dict.get(key)}\n'
            updated_file_contents += line
    with open(file_path, 'w') as updated_file:
        updated_file.write(updated_file_contents)

def update_params_env(workspace_id, region, params_env_file_path):
    original_to_replacement_dict = {}
    original_to_replacement_dict['workspaceId'] = workspace_id
    original_to_replacement_dict['workspaceRegion'] = region
    replace_params_env_in_line(params_env_file_path, original_to_replacement_dict)

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

def patch_notebook_for_prometheus():
    merge_patch_service_command = f'kubectl patch service notebook-controller-service --type=merge --patch-file={prometheus_yaml_files_directory}/notebook-controller-service-patch.yaml --namespace=kubeflow'.split()
    subprocess.call(merge_patch_service_command, encoding="utf-8")
    patch_deployment_command = f'kubectl patch deployment notebook-controller-deployment --patch-file {prometheus_yaml_files_directory}/notebook-controller-deployment-patch.yaml --namespace kubeflow'.split()
    subprocess.call(patch_deployment_command, encoding="utf-8")

def set_up_prometheus_for_AMP(cluster_name, region):    
    associate_iam_oidc_provider(cluster_name, region)

    policy_arn = create_AMP_ingest_policy()
    
    # Create AMP service account:
    create_AMP_service_account_command = f'eksctl create iamserviceaccount --name {SERVICE_ACCOUNT_AMP_INGEST_NAME} --role-name {SERVICE_ACCOUNT_IAM_AMP_INGEST_ROLE} --namespace {PROMETHEUS_NAMESPACE} --cluster {cluster_name} --attach-policy-arn {policy_arn} --override-existing-serviceaccounts --approve --region {region}'.split()
    subprocess.call(create_AMP_service_account_command, encoding="utf-8")

    workspace_id = create_AMP_workspace(region)
    
    # Edit params.env to use workspace-id and region
    update_params_env(workspace_id, region, f'{prometheus_yaml_files_directory}/params.env')
    
    create_namespace_command = f'kubectl create namespace {PROMETHEUS_NAMESPACE}'.split()
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

    patch_notebook_for_prometheus()
    
    return workspace_id

def set_up_prometheus_port_forwarding():
    print("Entered prometheus port_forwarding_function.")
    def get_prometheus_pod_name():
        get_pod_name_command = f'kubectl get pods --namespace={PROMETHEUS_NAMESPACE}'.split()
        print(' '.join(get_pod_name_command))
        pod_list = subprocess.check_output(get_pod_name_command, encoding="utf-8").split('\n')[1:]
        print(f"{PROMETHEUS_NAMESPACE} Pods list: {pod_list}")
        assert 0 < len(pod_list)
        prometheus_pod_name = None
        for pod in pod_list:
            pod_name = pod.split()[0]
            print("pod name and status:", pod)
            pod_name_array = pod_name.split('-')
            if (('p' == pod_name[0])
                and ('prometheus' == pod_name_array[0])
                and ('deployment' == pod_name_array[1])):
                describe_pod_command = f'kubectl describe pod {pod_name} --namespace {PROMETHEUS_NAMESPACE}'.split()
                print('\n\nAbout to call describe pod command!\n\n')
                print(subprocess.check_output(describe_pod_command, encoding="utf-8"))
                print('\n\nDescribe Finished\n\n')
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

    set_up_port_forwarding_command = f'kubectl port-forward {prometheus_pod_name} {local_prometheus_port_forwarding_port}:9090 -n {PROMETHEUS_NAMESPACE}'.split()
    print(" ".join(set_up_port_forwarding_command))
    port_forwarding_process = subprocess.Popen(set_up_port_forwarding_command)
    time.sleep(40)  # Wait 40 seconds for port forwarding to open and prometheus to start scraping
    return port_forwarding_process
    
def check_prometheus_is_running():
    check_up_command = f'curl http://localhost:{local_prometheus_port_forwarding_port}/api/v1/query?query=up'.split()
    print(' '.join(check_up_command))
    up_results = subprocess.check_output(check_up_command, encoding="utf-8")
    assert True==bool(up_results)

def get_prometheus_query_results(prometheus_query=default_prometheus_query):
    prometheus_curl_command = f"curl http://localhost:{local_prometheus_port_forwarding_port}/api/v1/query?query={prometheus_query}".split()
    print(f"Prometheus curl command:\n{' '.join(prometheus_curl_command)}")
    prometheus_query_results = subprocess.check_output(prometheus_curl_command, encoding="utf-8")
    print(f"Prometheus query results:\n{prometheus_query_results}")
    resulting_count = prometheus_query_results.split(",")[-1].split('"')[1]
    print(f"{prometheus_query}:", resulting_count)
    return resulting_count
    
def check_AMP_connects_to_prometheus(region, workspace_id, expected_value, prometheus_query=default_prometheus_query):
    
    time.sleep(60) # Wait for prometheus to scrape.
    access_key = os.environ['AWS_ACCESS_KEY_ID']
    secret_key = os.environ['AWS_SECRET_ACCESS_KEY']
    # AMP queries do not take {} arguments.
    AMP_query_components = re.split('\\\{|="|",|"\\\}',prometheus_query)
    AMP_query = AMP_query_components[0]
    AMP_query_specifics = AMP_query_components[1:-1]
    AMP_specifics = {}
    for i in range(1,len(AMP_query_specifics),2):
        AMP_specifics[AMP_query_specifics[i-1]]=AMP_query_specifics[i]

    print(f"Using Workspace ID: {workspace_id}")
    
    AMP_awscurl_command = f'awscurl --access_key {access_key} --secret_key {secret_key} --region {region} --service aps https://aps-workspaces.{region}.amazonaws.com/workspaces/{workspace_id}/api/v1/query?query={AMP_query}'.split()

    print(f'AMP awscurl command:\n{" ".join(AMP_awscurl_command)}')
    AMP_query_results = subprocess.check_output(AMP_awscurl_command, encoding="utf-8")

    broad_results = json.loads(AMP_query_results)
    for specific_result in broad_results["data"]["result"]:
        found_result = True
        for key in AMP_specifics:
            if (specific_result["metric"][key] != AMP_specifics[key]):
                found_result = False
                break
        if found_result:
            AMP_query_results=json.dumps(specific_result)
            break
    
    print(f"AMP_query_results:\n{AMP_query_results}")
    AMP_resulting_count = AMP_query_results.split(",")[-1].split('"')[1]
    print("AMP_resulting_count:", AMP_resulting_count)

    prometheus_resulting_count = get_prometheus_query_results(prometheus_query)
    
    print(f"Asserting AMP == prometheus: {AMP_resulting_count} == {prometheus_resulting_count}")
    assert AMP_resulting_count == prometheus_resulting_count
    print(f"Asserting expected == prometheus: {expected_value} == {prometheus_resulting_count}")
    assert str(expected_value) == prometheus_resulting_count
    return AMP_resulting_count

def delete_AMP_resources(cluster_name, region, workspace_id):
    print("About to delete service account and role.")
    delete_AMP_service_account_command = f'eksctl delete iamserviceaccount --name amp-iamproxy-ingest-service-account --namespace {PROMETHEUS_NAMESPACE} --cluster {cluster_name} --region {region}'.split()
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
