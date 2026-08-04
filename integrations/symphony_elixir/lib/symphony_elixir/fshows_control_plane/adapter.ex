defmodule SymphonyElixir.FshowsControlPlane.Adapter do
  @moduledoc """
  Tracker adapter for the Fshows Agent Control Plane.

  Candidate reads remain side-effect free. The dispatch revalidation performed
  by Symphony calls `fetch_issues_by_ids/1`; the client atomically claims a
  Ready WorkItem at that boundary and heartbeats claims owned by this host.
  """

  @behaviour SymphonyElixir.Tracker

  alias SymphonyElixir.FshowsControlPlane.{AgentTool, Client}
  alias SymphonyElixir.Tracker.Issue

  @active_states ["ready", "running"]
  @terminal_states ["done", "cancelled"]

  @impl true
  def validate_config(tracker_settings) do
    with :ok <- validate_states(tracker_settings.active_states, @active_states, :active),
         :ok <- validate_states(tracker_settings.terminal_states, @terminal_states, :terminal) do
      Client.validate_settings(tracker_settings)
    end
  end

  @impl true
  def fetch_issues_by_states(states), do: client_module().fetch_issues_by_states(states)

  @impl true
  def fetch_issues_by_ids(issue_ids), do: client_module().fetch_issues_by_ids(issue_ids)

  @impl true
  def agent_tool_specs, do: AgentTool.tool_specs()

  @impl true
  def execute_agent_tool(tool, arguments, opts), do: AgentTool.execute(tool, arguments, opts)

  @impl true
  def secret_environment_names(tracker_settings),
    do: Client.secret_environment_names(tracker_settings)

  defp client_module do
    Application.get_env(:symphony_elixir, :fshows_control_plane_client_module, Client)
  end

  defp validate_states(states, allowed, kind) when is_list(states) do
    normalized = Enum.map(states, &normalize_state/1)

    cond do
      normalized == [] -> {:error, {:missing_fshows_control_plane_states, kind}}
      not Enum.all?(normalized, &(&1 in allowed)) -> {:error, {:invalid_fshows_control_plane_states, kind}}
      kind == :active and not Enum.all?(@active_states, &(&1 in normalized)) ->
        {:error, :fshows_control_plane_active_states_must_include_ready_and_running}

      true -> :ok
    end
  end

  defp validate_states(_states, _allowed, kind),
    do: {:error, {:missing_fshows_control_plane_states, kind}}

  defp normalize_state(value) when is_binary(value),
    do: value |> String.trim() |> String.downcase()

  defp normalize_state(_value), do: ""
end
