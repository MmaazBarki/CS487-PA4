import azure.functions as func
import azure.durable_functions as df
import os, json, time, requests

app = df.DFApp(http_auth_level=func.AuthLevel.FUNCTION)

@app.route(route="orchestrators/my_orchestrator", methods=["POST"])
@app.durable_client_input(client_name="client")
async def http_starter(req: func.HttpRequest, client: df.DurableOrchestrationClient):
    order = req.get_json()
    instance_id = await client.start_new("my_orchestrator", client_input=order)
    return client.create_check_status_response(req, instance_id)

@app.orchestration_trigger(context_name="context")
def my_orchestrator(context: df.DurableOrchestrationContext):
    order = context.get_input()
    
    # 1. Call validate_activity
    # This will now throw an exception if the URL is missing or 404s
    validation_result = yield context.call_activity("validate_activity", order)
    
    # 2. Check validation
    if not validation_result or not validation_result.get("valid"):
        return {"status": "rejected", "reason": validation_result.get("reason", "Validation failed")}
    
    # 3. Proceed to report
    report_url = yield context.call_activity("report_activity", order)
    return {"status": "completed", "report_url": report_url}
    # pass

@app.activity_trigger(input_name="order")
def validate_activity(order: dict) -> dict:
    validate_url = os.environ.get("VALIDATE_URL")
    
    # Safety check: If URL isn't set yet (Task 5), return a clear message
    if not validate_url:
        return {"valid": False, "reason": "VALIDATE_URL environment variable is not set."}
    
    try:
        response = requests.post(validate_url, json=order, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        # Returns a dict so the orchestrator doesn't crash with "not subscriptable"
        return {"valid": False, "reason": str(e)}
    # pass
    

@app.activity_trigger(input_name="order")
def report_activity(order: dict) -> str:
    from azure.mgmt.containerinstance import ContainerInstanceManagementClient
    from azure.mgmt.containerinstance.models import (
        ContainerGroup, Container, ResourceRequirements, ResourceRequests,
        ImageRegistryCredential, EnvironmentVariable, OperatingSystemTypes,
        ContainerGroupRestartPolicy, ContainerGroupIdentity, ResourceIdentityType
    )
    from azure.identity import DefaultAzureCredential

    sub_id   = os.environ["SUBSCRIPTION_ID"]
    rg       = os.environ["REPORT_RG"]
    loc      = os.environ["REPORT_LOCATION"]
    image    = os.environ["REPORT_IMAGE"]
    order_id = order["order_id"]
    name     = f"ci-report-{order_id.lower()}"

    client = ContainerInstanceManagementClient(DefaultAzureCredential(), sub_id)
    
    # Construct the Managed Identity Resource ID
    rollnum = rg.split("-")[-1]
    mi_id = f"/subscriptions/{sub_id}/resourcegroups/{rg}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/mi-pa4-{rollnum}"
    
    # Create the container group
    # Replace the `None` values below with the correct properties.
    # Hint: Follow the structure shown in the skeleton.
    # Create the container group
    
    group = ContainerGroup(
        location=loc, 
        os_type=OperatingSystemTypes.linux,
        restart_policy=ContainerGroupRestartPolicy.never,
        identity=ContainerGroupIdentity(
            type=ResourceIdentityType.user_assigned,
            user_assigned_identities={mi_id: {}}
        ),
        image_registry_credentials=[ImageRegistryCredential(
            server=os.environ["ACR_SERVER"],
            username=os.environ["ACR_USERNAME"],
            password=os.environ["ACR_PASSWORD"])],
        containers=[Container(
            name="report", image=image,
            resources=ResourceRequirements(
                requests=ResourceRequests(cpu=1.0, memory_in_gb=1.5)),
            environment_variables=[
                EnvironmentVariable(name="ORDER_ID", value=order["order_id"]),
                EnvironmentVariable(name="ORDER_JSON", value=json.dumps(order)),
                EnvironmentVariable(name="STORAGE_ACCOUNT_URL", value=os.environ["STORAGE_ACCOUNT_URL"]),
                EnvironmentVariable(name="AZURE_CLIENT_ID", value=os.environ["AZURE_CLIENT_ID"])
            ])]
    )
    
    client.container_groups.begin_create_or_update(rg, name, group).result()

    # Poll until Succeeded (or 5 min max)
    for _ in range(60):
        info = client.container_groups.get(rg, name)
        state = info.instance_view.state if info.instance_view else None
        if state in ("Succeeded", "Failed"):
            break
        time.sleep(5)

    # Clean up so it stops being a visible resource
    client.container_groups.begin_delete(rg, name)

    # In case STORAGE_ACCOUNT_URL is not set as an environment variable, try to parse it from STORAGE_CONN,
    # or use it directly as done previously.
    storage_url = os.environ.get("STORAGE_ACCOUNT_URL", "https://<your-storage-account>.blob.core.windows.net")
    return f"{storage_url}/reports/{order_id}.pdf"
    # pass
