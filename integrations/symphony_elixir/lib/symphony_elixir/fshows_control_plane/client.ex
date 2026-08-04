defmodule SymphonyElixir.FshowsControlPlane.Client do
  @moduledoc """
  Host-side HTTP client, WorkItem normalizer, and claim-token owner.

  Claim tokens are stored in a private ETS table and are never added to an
  Issue, prompt, dynamic-tool arguments, or child-process environment.
  """

  require Logger

  alias SymphonyElixir.Config
  alias SymphonyElixir.Tracker.Issue

  @claim_table :symphony_fshows_control_plane_claims
  @default_endpoint "http://127.0.0.1:8080"
  @default_lease_seconds 300

  @spec validate_settings(map()) :: :ok | {:error, term()}
  def validate_settings(tracker_settings) do
    with {:ok, _settings} <- settings(tracker_settings), do: :ok
  end

  @spec secret_environment_names(map()) :: [String.t()]
  def secret_environment_names(tracker_settings) do
    provider = provider_settings(tracker_settings)

    ["CONTROL_PLANE_TOKEN" | env_reference_names([provider["token"]])]
    |> Enum.uniq()
  end

  @spec fetch_issues_by_states([String.t()]) :: {:ok, [Issue.t()]} | {:error, term()}
  def fetch_issues_by_states(states) when is_list(states) do
    fetch_issues_by_states(states, Config.settings!().tracker, &perform_request/5)
  end

  @spec fetch_issues_by_ids([String.t()]) :: {:ok, [Issue.t()]} | {:error, term()}
  def fetch_issues_by_ids(issue_ids) when is_list(issue_ids) do
    fetch_issues_by_ids(issue_ids, Config.settings!().tracker, &perform_request/5)
  end

  @spec get_work_item(String.t(), keyword()) :: {:ok, map()} | {:error, term()}
  def get_work_item(work_item_id, opts) when is_binary(work_item_id) and is_list(opts) do
    request_work_item("GET", work_item_path(work_item_id), %{}, opts)
  end

  @spec add_event(String.t(), map(), keyword()) :: {:ok, map()} | {:error, term()}
  def add_event(work_item_id, payload, opts)
      when is_binary(work_item_id) and is_map(payload) and is_list(opts) do
    with {:ok, token} <- claim_token(work_item_id) do
      request_work_item(
        "POST",
        work_item_path(work_item_id) <> "/events",
        Map.put(payload, "claim_token", token),
        opts
      )
    end
  end

  @spec add_artifact(String.t(), map(), keyword()) :: {:ok, map()} | {:error, term()}
  def add_artifact(work_item_id, payload, opts)
      when is_binary(work_item_id) and is_map(payload) and is_list(opts) do
    with {:ok, token} <- claim_token(work_item_id) do
      request_work_item(
        "POST",
        work_item_path(work_item_id) <> "/artifacts",
        Map.put(payload, "claim_token", token),
        opts
      )
    end
  end

  @spec request_human(String.t(), map(), keyword()) :: {:ok, map()} | {:error, term()}
  def request_human(work_item_id, payload, opts)
      when is_binary(work_item_id) and is_map(payload) and is_list(opts) do
    with {:ok, token} <- claim_token(work_item_id),
         {:ok, response} <-
           request_work_item(
             "POST",
             work_item_path(work_item_id) <> "/decisions",
             payload
             |> Map.put("action", "request")
             |> Map.put("claim_token", token),
             opts
           ) do
      forget_claim(work_item_id)
      {:ok, response}
    end
  end

  @spec complete(String.t(), keyword()) :: {:ok, map()} | {:error, term()}
  def complete(work_item_id, opts) when is_binary(work_item_id) and is_list(opts) do
    with {:ok, token} <- claim_token(work_item_id),
         {:ok, body} <-
           request_work_item(
             "POST",
             work_item_path(work_item_id) <> "/status",
             %{
               "to_status" => "stage_review",
               "event" => "agent_completed",
               "actor_type" => "agent",
               "actor_id" => "codex",
               "claim_token" => token
             },
             opts
           ) do
      forget_claim(work_item_id)
      {:ok, body}
    end
  end

  @spec block(String.t(), map(), keyword()) :: {:ok, map()} | {:error, term()}
  def block(work_item_id, blocker, opts)
      when is_binary(work_item_id) and is_map(blocker) and is_list(opts) do
    with {:ok, token} <- claim_token(work_item_id),
         {:ok, body} <-
           request_work_item(
             "POST",
             work_item_path(work_item_id) <> "/status",
             %{
               "to_status" => "blocked",
               "event" => "work_item_blocked",
               "actor_type" => "agent",
               "actor_id" => "codex",
               "claim_token" => token,
               "payload" => %{"blocker" => blocker}
             },
             opts
           ) do
      forget_claim(work_item_id)
      {:ok, body}
    end
  end

  @spec claim_token(String.t()) :: {:ok, String.t()} | {:error, :claim_not_owned}
  def claim_token(work_item_id) when is_binary(work_item_id) do
    case :ets.lookup(claim_table(), work_item_id) do
      [{^work_item_id, token}] when is_binary(token) -> {:ok, token}
      _ -> {:error, :claim_not_owned}
    end
  end

  @spec forget_claim(String.t()) :: :ok
  def forget_claim(work_item_id) when is_binary(work_item_id) do
    :ets.delete(claim_table(), work_item_id)
    :ok
  end

  @doc false
  @spec reset_claims_for_test() :: :ok
  def reset_claims_for_test do
    :ets.delete_all_objects(claim_table())
    :ok
  end

  @doc false
  @spec fetch_issues_by_states_for_test([String.t()], map(), function()) ::
          {:ok, [Issue.t()]} | {:error, term()}
  def fetch_issues_by_states_for_test(states, tracker_settings, request_fun)
      when is_list(states) and is_map(tracker_settings) and is_function(request_fun, 5) do
    fetch_issues_by_states(states, tracker_settings, request_fun)
  end

  @doc false
  @spec fetch_issues_by_ids_for_test([String.t()], map(), function()) ::
          {:ok, [Issue.t()]} | {:error, term()}
  def fetch_issues_by_ids_for_test(ids, tracker_settings, request_fun)
      when is_list(ids) and is_map(tracker_settings) and is_function(request_fun, 5) do
    fetch_issues_by_ids(ids, tracker_settings, request_fun)
  end

  defp fetch_issues_by_states(state_names, tracker_settings, request_fun) do
    states = state_names |> Enum.map(&normalize_state/1) |> Enum.reject(&(&1 == "")) |> Enum.uniq()

    with {:ok, resolved} <- settings(tracker_settings) do
      Enum.reduce_while(states, {:ok, []}, fn state, {:ok, acc} ->
        path = if state == "ready", do: "/api/work-items/candidates", else: "/api/work-items"
        params = if state == "ready", do: %{}, else: %{"status" => state}

        case api_request("GET", path, params, nil, resolved, request_fun) do
          {:ok, %{status: 200, body: body}} when is_list(body) ->
            issues = body |> Enum.map(&normalize_work_item(&1, resolved, false)) |> Enum.reject(&is_nil/1)
            {:cont, {:ok, acc ++ issues}}

          {:ok, %{status: status}} ->
            {:halt, {:error, {:fshows_control_plane_status, status}}}

          {:error, reason} ->
            {:halt, {:error, reason}}
        end
      end)
      |> then(fn
        {:ok, issues} -> {:ok, Enum.uniq_by(issues, & &1.id)}
        error -> error
      end)
    end
  end

  defp fetch_issues_by_ids(issue_ids, tracker_settings, request_fun) do
    with {:ok, resolved} <- settings(tracker_settings) do
      issue_ids
      |> Enum.uniq()
      |> Enum.reduce_while({:ok, []}, fn issue_id, {:ok, acc} ->
        case refresh_or_claim(issue_id, resolved, request_fun) do
          {:ok, nil} -> {:cont, {:ok, acc}}
          {:ok, issue} -> {:cont, {:ok, [issue | acc]}}
          {:error, reason} -> {:halt, {:error, reason}}
        end
      end)
      |> case do
        {:ok, issues} -> {:ok, Enum.reverse(issues)}
        error -> error
      end
    end
  end

  defp refresh_or_claim(issue_id, settings, request_fun) do
    path = work_item_path(issue_id)

    case api_request("GET", path, %{}, nil, settings, request_fun) do
      {:ok, %{status: 404}} -> {:ok, nil}
      {:ok, %{status: 200, body: %{"status" => "ready"} = item}} -> claim_ready(item, settings, request_fun)
      {:ok, %{status: 200, body: %{"status" => "running"} = item}} -> heartbeat_owned(item, settings, request_fun)
      {:ok, %{status: 200, body: item}} when is_map(item) -> {:ok, normalize_work_item(item, settings, false)}
      {:ok, %{status: status}} -> {:error, {:fshows_control_plane_status, status}}
      {:error, reason} -> {:error, reason}
    end
  end

  defp claim_ready(item, settings, request_fun) do
    body = %{
      "workerId" => settings.worker_id,
      "expectedVersion" => item["version"],
      "leaseSeconds" => settings.lease_seconds
    }

    case api_request("POST", work_item_path(item["id"]) <> "/claim", %{}, body, settings, request_fun) do
      {:ok, %{status: 200, body: %{"work_item" => claimed, "claim_token" => token}}}
      when is_binary(token) ->
        :ets.insert(claim_table(), {item["id"], token})
        {:ok, normalize_work_item(claimed, settings, true)}

      {:ok, %{status: 409}} ->
        refresh_after_conflict(item["id"], settings, request_fun)

      {:ok, %{status: status}} ->
        {:error, {:fshows_control_plane_claim_status, status}}

      {:error, reason} ->
        {:error, reason}
    end
  end

  defp heartbeat_owned(item, settings, request_fun) do
    case claim_token(item["id"]) do
      {:ok, token} ->
        body = %{"claimToken" => token, "leaseSeconds" => settings.lease_seconds}

        case api_request("POST", work_item_path(item["id"]) <> "/heartbeat", %{}, body, settings, request_fun) do
          {:ok, %{status: 200, body: refreshed}} ->
            {:ok, normalize_work_item(refreshed, settings, true)}

          {:ok, %{status: 409}} ->
            forget_claim(item["id"])
            refresh_after_conflict(item["id"], settings, request_fun)

          {:ok, %{status: status}} ->
            {:error, {:fshows_control_plane_heartbeat_status, status}}

          {:error, reason} ->
            {:error, reason}
        end

      {:error, :claim_not_owned} ->
        {:ok, normalize_work_item(item, settings, false)}
    end
  end

  defp refresh_after_conflict(issue_id, settings, request_fun) do
    case api_request("GET", work_item_path(issue_id), %{}, nil, settings, request_fun) do
      {:ok, %{status: 200, body: item}} -> {:ok, normalize_work_item(item, settings, false)}
      {:ok, %{status: 404}} -> {:ok, nil}
      {:ok, %{status: status}} -> {:error, {:fshows_control_plane_status, status}}
      {:error, reason} -> {:error, reason}
    end
  end

  defp normalize_work_item(item, settings, claim_owned) when is_map(item) do
    id = item["id"]
    title = item["title"]
    state = item["status"]

    if present_string?(id) and present_string?(title) and present_string?(state) do
      blocked_by =
        item
        |> Map.get("blocked_by", [])
        |> Enum.filter(&is_binary/1)
        |> Enum.map(&%{"id" => &1, "identifier" => &1})

      %Issue{
        id: id,
        native_ref: %{
          "work_item_id" => id,
          "feature_id" => item["feature_id"],
          "version" => item["version"],
          "agent_role" => item["agent_role"],
          "stage" => item["stage"]
        },
        identifier: id,
        title: title,
        description: item["description"],
        priority: normalize_priority(item["priority"]),
        state: state,
        branch_name: get_in(item, ["repository", "head_branch"]),
        url: settings.endpoint <> work_item_path(id),
        assignee_id: get_in(item, ["claim", "worker_id"]),
        blocked_by: blocked_by,
        labels: ["agent/#{item["agent_role"]}", "stage/#{item["stage"]}"],
        dispatchable: blocked_by == [] and (state == "ready" or (state == "running" and claim_owned)),
        created_at: parse_datetime(item["created_at"]),
        updated_at: parse_datetime(item["updated_at"])
      }
    end
  end

  defp normalize_work_item(_item, _settings, _claim_owned), do: nil

  defp request_work_item(method, path, body, opts) do
    tracker_settings = Keyword.get_lazy(opts, :tracker_settings, fn -> Config.settings!().tracker end)
    request_fun = Keyword.get(opts, :request_fun, &perform_request/5)

    with {:ok, resolved} <- settings(tracker_settings),
         {:ok, %{status: status, body: response_body}} <-
           api_request(method, path, %{}, body, resolved, request_fun) do
      if status in 200..299,
        do: {:ok, response_body},
        else: {:error, {:fshows_control_plane_status, status, response_body}}
    end
  end

  defp api_request(method, path, params, body, settings, request_fun) do
    case request_fun.(method, path, params, body, settings) do
      {:ok, %{status: status, body: _body} = response} when is_integer(status) -> {:ok, response}
      {:error, reason} -> {:error, reason}
      _ -> {:error, :fshows_control_plane_unknown_payload}
    end
  end

  defp perform_request(method, path, params, body, settings) do
    opts = [
      method: request_method(method),
      url: settings.endpoint <> path,
      headers: [{"authorization", "Bearer #{settings.token}"}],
      params: params,
      connect_options: [timeout: 30_000]
    ]

    opts = if is_nil(body), do: opts, else: Keyword.put(opts, :json, body)

    case Req.request(opts) do
      {:ok, response} -> {:ok, %{status: response.status, body: response.body}}
      {:error, reason} -> {:error, {:fshows_control_plane_request, reason}}
    end
  end

  defp settings(tracker_settings) when is_map(tracker_settings) do
    provider = provider_settings(tracker_settings)
    endpoint =
      normalize_endpoint(
        provider["endpoint"] || @default_endpoint,
        provider["allow_insecure_http"] == true
      )
    token = resolve_setting(provider["token"], System.get_env("CONTROL_PLANE_TOKEN"))

    worker_id =
      resolve_setting(provider["worker_id"], System.get_env("SYMPHONY_WORKER_ID")) ||
        "symphony-#{node()}"

    lease_seconds = provider["lease_seconds"] || @default_lease_seconds

    cond do
      is_nil(endpoint) -> {:error, :invalid_fshows_control_plane_endpoint}
      not present_string?(token) -> {:error, :missing_fshows_control_plane_token}
      not present_string?(worker_id) -> {:error, :missing_fshows_control_plane_worker_id}
      not is_integer(lease_seconds) or lease_seconds < 10 or lease_seconds > 3600 ->
        {:error, :invalid_fshows_control_plane_lease_seconds}

      true ->
        {:ok,
         %{
           endpoint: endpoint,
           token: token,
           worker_id: worker_id,
           lease_seconds: lease_seconds
         }}
    end
  end

  defp provider_settings(%{provider: provider}) when is_map(provider), do: provider
  defp provider_settings(_tracker_settings), do: %{}

  defp normalize_endpoint(value, allow_insecure_http) when is_binary(value) do
    trimmed = String.trim_trailing(String.trim(value), "/")

    case URI.parse(trimmed) do
      %URI{scheme: "https", host: host} when is_binary(host) -> trimmed
      %URI{scheme: "http", host: host} when host in ["127.0.0.1", "localhost", "::1"] -> trimmed
      %URI{scheme: "http", host: host} when is_binary(host) and allow_insecure_http -> trimmed
      _ -> nil
    end
  end

  defp normalize_endpoint(_value, _allow_insecure_http), do: nil

  defp resolve_setting(nil, fallback), do: normalize_string(fallback)

  defp resolve_setting("$" <> env_name, fallback) do
    if valid_env_name?(env_name),
      do: normalize_string(System.get_env(env_name) || fallback),
      else: nil
  end

  defp resolve_setting(value, _fallback), do: normalize_string(value)

  defp normalize_string(value) when is_binary(value) do
    case String.trim(value) do
      "" -> nil
      trimmed -> trimmed
    end
  end

  defp normalize_string(_value), do: nil

  defp env_reference_names(values) do
    Enum.flat_map(values, fn
      "$" <> name -> if valid_env_name?(name), do: [name], else: []
      _ -> []
    end)
  end

  defp valid_env_name?(name), do: String.match?(name, ~r/^[A-Za-z_][A-Za-z0-9_]*$/)
  defp present_string?(value) when is_binary(value), do: String.trim(value) != ""
  defp present_string?(_value), do: false

  defp normalize_state(value) when is_binary(value),
    do: value |> String.trim() |> String.downcase()

  defp normalize_state(_value), do: ""
  defp normalize_priority(value) when is_integer(value), do: min(max(value + 1, 1), 4)
  defp normalize_priority(_value), do: nil

  defp parse_datetime(value) when is_binary(value) do
    case DateTime.from_iso8601(value) do
      {:ok, datetime, _offset} -> datetime
      _ -> nil
    end
  end

  defp parse_datetime(_value), do: nil
  defp request_method("GET"), do: :get
  defp request_method("POST"), do: :post
  defp request_method("PATCH"), do: :patch
  defp request_method("DELETE"), do: :delete
  defp work_item_path(id), do: "/api/work-items/#{URI.encode(id, &URI.char_unreserved?/1)}"

  defp claim_table do
    case :ets.whereis(@claim_table) do
      :undefined ->
        try do
          :ets.new(@claim_table, [:named_table, :set, :public, read_concurrency: true])
        rescue
          ArgumentError -> @claim_table
        end

      table ->
        table
    end
  end
end
