defmodule SymphonyElixir.FshowsControlPlaneAdapterTest do
  use SymphonyElixir.TestSupport

  alias SymphonyElixir.Config.Schema.Tracker
  alias SymphonyElixir.FshowsControlPlane.{Adapter, AgentTool, Client}
  alias SymphonyElixir.Tracker.Issue

  defmodule FakeToolClient do
    @spec get_work_item(String.t(), keyword()) :: {:ok, map()}
    def get_work_item(id, opts) do
      send(self(), {:get_work_item, id, opts})
      {:ok, %{"id" => id}}
    end

    @spec complete(String.t(), keyword()) :: {:ok, map()}
    def complete(id, opts) do
      send(self(), {:complete, id, opts})
      {:ok, %{"id" => id, "status" => "stage_review"}}
    end
  end

  setup do
    Client.reset_claims_for_test()
    :ok
  end

  test "adapter validates states and host-only auth settings" do
    assert :ok = Adapter.validate_config(tracker_settings())

    assert {:error, :fshows_control_plane_active_states_must_include_ready_and_running} =
             Adapter.validate_config(%{tracker_settings() | active_states: ["ready"]})

    assert {:error, {:invalid_fshows_control_plane_states, :terminal}} =
             Adapter.validate_config(%{tracker_settings() | terminal_states: ["closed"]})

    assert Client.secret_environment_names(tracker_settings()) == [
             "CONTROL_PLANE_TOKEN"
           ]

    assert Client.secret_environment_names(
             tracker_settings(%{"token" => "$FSHOWS_CONTROL_PLANE_TOKEN"})
           ) == ["CONTROL_PLANE_TOKEN", "FSHOWS_CONTROL_PLANE_TOKEN"]
  end

  test "candidate read is side-effect free and dispatch refresh atomically claims" do
    ready = work_item("ready", 7)
    running = work_item("running", 8)
    test_pid = self()

    request_fun = fn
      "GET", "/api/work-items/candidates", %{}, nil, settings ->
        send(test_pid, {:candidates, settings})
        {:ok, %{status: 200, body: [ready]}}

      "GET", "/api/work-items/WI-001", %{}, nil, _settings ->
        send(test_pid, :refresh)
        {:ok, %{status: 200, body: ready}}

      "POST", "/api/work-items/WI-001/claim", %{}, body, _settings ->
        send(test_pid, {:claim, body})
        {:ok, %{status: 200, body: %{"work_item" => running, "claim_token" => "host-secret-token"}}}

      "POST", "/api/work-items/WI-001/events", %{}, body, _settings ->
        send(test_pid, {:event, body})
        {:ok, %{status: 201, body: %{"id" => "event-1"}}}

      "POST", "/api/work-items/WI-001/artifacts", %{}, body, _settings ->
        send(test_pid, {:artifact, body})
        {:ok, %{status: 201, body: %{"id" => "artifact-1"}}}
    end

    assert {:ok, [%Issue{state: "ready", dispatchable: true}]} =
             Client.fetch_issues_by_states_for_test(
               ["ready"],
               tracker_settings(),
               request_fun
             )

    assert_receive {:candidates, %{token: "test-token"}}
    refute_receive {:claim, _body}

    assert {:ok, [%Issue{} = issue]} =
             Client.fetch_issues_by_ids_for_test(
               ["WI-001"],
               tracker_settings(),
               request_fun
             )

    assert issue.state == "running"
    assert issue.dispatchable
    assert issue.labels == ["agent/solution_architect", "stage/tech_analysis"]
    refute Map.has_key?(issue.native_ref, "claim_token")
    assert_receive :refresh

    assert_receive {:claim,
                    %{
                      "workerId" => "symphony-test",
                      "expectedVersion" => 7,
                      "leaseSeconds" => 60
                    }}

    assert {:ok, "host-secret-token"} = Client.claim_token("WI-001")

    assert {:ok, %{"id" => "event-1"}} =
             Client.add_event(
               "WI-001",
               %{"event_type" => "agent_started"},
               tracker_settings: tracker_settings(),
               request_fun: request_fun
             )

    assert_receive {:event,
                    %{
                      "event_type" => "agent_started",
                      "claim_token" => "host-secret-token"
                    }}

    assert {:ok, %{"id" => "artifact-1"}} =
             Client.add_artifact(
               "WI-001",
               %{"direction" => "output", "path" => "handoff.yaml", "revision" => "abc"},
               tracker_settings: tracker_settings(),
               request_fun: request_fun
             )

    assert_receive {:artifact, %{"claim_token" => "host-secret-token"}}
  end

  test "a foreign Running claim is visible but not dispatchable" do
    running = work_item("running", 8)

    request_fun = fn "GET", "/api/work-items/WI-001", %{}, nil, _settings ->
      {:ok, %{status: 200, body: running}}
    end

    assert {:ok, [%Issue{state: "running", dispatchable: false}]} =
             Client.fetch_issues_by_ids_for_test(
               ["WI-001"],
               tracker_settings(),
               request_fun
             )
  end

  test "agent tools bind to the current issue and never accept a target id or token" do
    names = Enum.map(AgentTool.tool_specs(), & &1["name"])

    assert names == [
             "work_item_get",
             "work_item_add_event",
             "work_item_add_artifact",
             "work_item_request_human",
             "work_item_complete",
             "work_item_block"
           ]

    refute AgentTool.tool_specs()
           |> Jason.encode!()
           |> String.contains?("claim_token")

    response =
      AgentTool.execute(
        "work_item_get",
        %{},
        issue: %Issue{id: "WI-001"},
        tracker_settings: tracker_settings(),
        fshows_control_plane_client: FakeToolClient
      )

    assert response["success"]
    assert_receive {:get_work_item, "WI-001", tracker_settings: %Tracker{}}

    rejected =
      AgentTool.execute(
        "work_item_get",
        %{"work_item_id" => "WI-999"},
        issue: %Issue{id: "WI-001"},
        fshows_control_plane_client: FakeToolClient
      )

    refute rejected["success"]
    refute_receive {:get_work_item, "WI-999", _opts}
  end

  defp tracker_settings(provider_overrides \\ %{}) do
    %Tracker{
      kind: "fshows_control_plane",
      provider:
        Map.merge(
          %{
            "endpoint" => "http://127.0.0.1:8080",
            "token" => "test-token",
            "worker_id" => "symphony-test",
            "lease_seconds" => 60
          },
          provider_overrides
        ),
      active_states: ["ready", "running"],
      terminal_states: ["done", "cancelled"]
    }
  end

  defp work_item(status, version) do
    %{
      "id" => "WI-001",
      "feature_id" => "FEATURE-001",
      "title" => "Architecture",
      "description" => "Analyze the feature",
      "stage" => "tech_analysis",
      "agent_role" => "solution_architect",
      "status" => status,
      "priority" => 1,
      "version" => version,
      "repository" => %{"head_branch" => "feature/FEATURE-001"},
      "dependencies" => [],
      "blocked_by" => [],
      "claim" => %{
        "worker_id" => if(status == "running", do: "symphony-test", else: nil),
        "expires_at" => if(status == "running", do: "2026-08-04T01:00:00Z", else: nil)
      },
      "created_at" => "2026-08-04T00:00:00Z",
      "updated_at" => "2026-08-04T00:01:00Z"
    }
  end
end
