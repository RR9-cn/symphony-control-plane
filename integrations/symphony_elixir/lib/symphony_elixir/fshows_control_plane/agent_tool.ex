defmodule SymphonyElixir.FshowsControlPlane.AgentTool do
  @moduledoc """
  Narrow host-side tools for the current WorkItem.

  The current WorkItem ID comes from Symphony's bound Issue rather than tool
  arguments, so an Agent cannot target an unrelated WorkItem. Claim tokens are
  resolved inside the host client and never appear in a tool schema.
  """

  alias SymphonyElixir.FshowsControlPlane.Client
  alias SymphonyElixir.Tracker.Issue

  @empty_schema %{"type" => "object", "additionalProperties" => false, "properties" => %{}}

  @spec execute(String.t() | nil, term(), keyword()) :: map()
  def execute(tool, arguments, opts) do
    with {:ok, work_item_id} <- current_work_item_id(opts),
         {:ok, operation, payload} <- normalize_operation(tool, arguments),
         {:ok, body} <- call_client(operation, work_item_id, payload, opts) do
      dynamic_tool_response(true, body)
    else
      {:error, reason} -> dynamic_tool_response(false, error_payload(reason))
    end
  end

  @spec tool_specs() :: [map()]
  def tool_specs do
    [
      tool("work_item_get", "Read the current Control Plane WorkItem.", @empty_schema),
      tool(
        "work_item_add_event",
        "Append an audit event to the current WorkItem.",
        object_schema(
          ["event_type"],
          %{
            "event_type" => %{"type" => "string", "minLength" => 1},
            "payload" => %{"type" => "object", "additionalProperties" => true}
          }
        )
      ),
      tool(
        "work_item_add_artifact",
        "Register an input or output Artifact for the current WorkItem.",
        object_schema(
          ["direction", "path", "revision"],
          %{
            "direction" => %{"type" => "string", "enum" => ["input", "output"]},
            "path" => %{"type" => "string", "minLength" => 1},
            "revision" => %{"type" => "string", "minLength" => 1},
            "media_type" => %{"type" => ["string", "null"]},
            "sha256" => %{"type" => ["string", "null"], "pattern" => "^[a-f0-9]{64}$"}
          }
        )
      ),
      tool(
        "work_item_request_human",
        "Request a structured human decision and move the current WorkItem to NeedsHuman.",
        object_schema(
          ["question"],
          %{
            "question" => %{"type" => "string", "minLength" => 1},
            "options" => %{
              "type" => "array",
              "items" => %{"type" => "string", "minLength" => 1}
            }
          }
        )
      ),
      tool(
        "work_item_complete",
        "Submit the current WorkItem to StageReview after its Handoff Artifact is registered.",
        @empty_schema
      ),
      tool(
        "work_item_block",
        "Record a blocker and release the current WorkItem claim.",
        object_schema(
          ["code", "message"],
          %{
            "code" => %{"type" => "string", "pattern" => "^[a-z][a-z0-9_]*$"},
            "message" => %{"type" => "string", "minLength" => 1}
          }
        )
      )
    ]
  end

  defp normalize_operation("work_item_get", arguments) when arguments in [nil, %{}],
    do: {:ok, :get_work_item, nil}

  defp normalize_operation("work_item_add_event", %{"event_type" => type} = arguments)
       when is_binary(type) do
    payload = %{
      "event_type" => String.trim(type),
      "actor_type" => "agent",
      "actor_id" => "codex",
      "payload" => Map.get(arguments, "payload", %{})
    }

    if payload["event_type"] != "" and is_map(payload["payload"]),
      do: {:ok, :add_event, payload},
      else: {:error, :invalid_event_arguments}
  end

  defp normalize_operation("work_item_add_artifact", arguments) when is_map(arguments) do
    direction = arguments["direction"]
    path = arguments["path"]
    revision = arguments["revision"]

    if direction in ["input", "output"] and valid_path?(path) and present_string?(revision) do
      {:ok, :add_artifact, Map.take(arguments, ["direction", "path", "revision", "media_type", "sha256"])}
    else
      {:error, :invalid_artifact_arguments}
    end
  end

  defp normalize_operation("work_item_request_human", %{"question" => question} = arguments)
       when is_binary(question) do
    options = Map.get(arguments, "options", [])

    if present_string?(question) and is_list(options) and Enum.all?(options, &present_string?/1) do
      {:ok, :request_human,
       %{
         "question" => String.trim(question),
         "options" => options,
         "actor_id" => "codex"
       }}
    else
      {:error, :invalid_human_request_arguments}
    end
  end

  defp normalize_operation("work_item_complete", arguments) when arguments in [nil, %{}],
    do: {:ok, :complete, nil}

  defp normalize_operation("work_item_block", %{"code" => code, "message" => message})
       when is_binary(code) and is_binary(message) do
    if String.match?(code, ~r/^[a-z][a-z0-9_]*$/) and present_string?(message) do
      {:ok, :block,
       %{
         "code" => code,
         "message" => String.trim(message),
         "since" => DateTime.utc_now() |> DateTime.to_iso8601()
       }}
    else
      {:error, :invalid_blocker_arguments}
    end
  end

  defp normalize_operation(tool, _arguments) when tool in [
         "work_item_get",
         "work_item_add_event",
         "work_item_add_artifact",
         "work_item_request_human",
         "work_item_complete",
         "work_item_block"
       ],
       do: {:error, :invalid_arguments}

  defp normalize_operation(tool, _arguments), do: {:error, {:unsupported_tool, tool}}

  defp call_client(operation, work_item_id, payload, opts) do
    client = Keyword.get(opts, :fshows_control_plane_client, Client)
    client_opts = Keyword.take(opts, [:tracker_settings, :request_fun])

    case operation do
      :get_work_item -> apply(client, :get_work_item, [work_item_id, client_opts])
      :add_event -> apply(client, :add_event, [work_item_id, payload, client_opts])
      :add_artifact -> apply(client, :add_artifact, [work_item_id, payload, client_opts])
      :request_human -> apply(client, :request_human, [work_item_id, payload, client_opts])
      :complete -> apply(client, :complete, [work_item_id, client_opts])
      :block -> apply(client, :block, [work_item_id, payload, client_opts])
    end
  end

  defp current_work_item_id(opts) do
    case Keyword.get(opts, :issue) do
      %Issue{id: id} when is_binary(id) and id != "" -> {:ok, id}
      _ -> {:error, :missing_bound_work_item}
    end
  end

  defp valid_path?(path) when is_binary(path) do
    present_string?(path) and not String.starts_with?(path, ["/", "\\"]) and
      not String.contains?(path, ["\\", <<0>>]) and
      path |> String.split("/") |> Enum.all?(&(&1 not in ["", ".", ".."]))
  end

  defp valid_path?(_path), do: false
  defp present_string?(value) when is_binary(value), do: String.trim(value) != ""
  defp present_string?(_value), do: false

  defp tool(name, description, schema) do
    %{"name" => name, "description" => description, "inputSchema" => schema}
  end

  defp object_schema(required, properties) do
    %{
      "type" => "object",
      "additionalProperties" => false,
      "required" => required,
      "properties" => properties
    }
  end

  defp dynamic_tool_response(success, payload) do
    output = encode(payload)
    %{"success" => success, "output" => output, "contentItems" => [%{"type" => "inputText", "text" => output}]}
  end

  defp error_payload({:unsupported_tool, tool}) do
    %{
      "error" => %{
        "message" => "Unsupported dynamic tool: #{inspect(tool)}.",
        "supportedTools" => Enum.map(tool_specs(), & &1["name"])
      }
    }
  end

  defp error_payload(:missing_bound_work_item),
    do: %{"error" => %{"message" => "The tool is not bound to a current WorkItem."}}

  defp error_payload(:claim_not_owned),
    do: %{"error" => %{"message" => "This Symphony host does not own the current WorkItem claim."}}

  defp error_payload(reason),
    do: %{"error" => %{"message" => "Control Plane tool execution failed.", "reason" => inspect(reason)}}

  defp encode(payload) do
    case Jason.encode(payload, pretty: true) do
      {:ok, value} -> value
      {:error, _reason} -> inspect(payload)
    end
  end
end
