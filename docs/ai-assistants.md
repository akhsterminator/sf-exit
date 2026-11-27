# Ask an AI assistant about your Salesforce data

`sf-exit mcp ./export` runs a [Model Context Protocol](https://modelcontextprotocol.io) server, so an AI assistant can answer questions from your exported org: "which accounts in EMEA have no open opportunities?", "summarise everything we know about Northwind", "who changed this opportunity's amount, and when?", "which custom fields look abandoned?".

It works on the export, not on Salesforce. That means no API limits, no credentials given to the assistant, and no way for it to change anything.

## Set-up

Run an export first, then add the server to your MCP client's configuration:

```json
{
  "mcpServers": {
    "salesforce": {
      "command": "sf-exit",
      "args": ["mcp", "/absolute/path/to/export"]
    }
  }
}
```

This is the standard configuration format used by MCP clients such as Cursor, VS Code, Zed and Claude Desktop. Check your client's documentation for where the file lives. The first start builds the local index (see [browse](browse.md#how-it-works)), which can take a while on a large export.

## Tools the assistant gets

| Tool | What it does |
|---|---|
| `list_objects` | Objects in the export with labels and row counts |
| `describe_object` | Fields of an object: labels, types, help text, picklist values, lookup targets |
| `query` | A read-only SQL query (SQLite dialect), capped at 200 rows |
| `search` | Full-text search across record names and key text fields |
| `get_record` | One record by Id, with lookups resolved, related counts and files |
| `get_record_history` | One record's timeline across backup runs: created, each update with old → new values, deleted ([time machine](time-machine.md)) |
| `audit_findings` | The findings from `sf-exit audit`, if it has been run |

## What the assistant can and can't do

- **Read-only.** The database connection is opened read-only, and only `SELECT` statements are accepted.
- **Local.** The server talks over stdin/stdout to the client that started it. It opens no network port and never contacts Salesforce.
- **Your data does go to the model.** Whatever the assistant reads is sent to the AI provider you use, under their terms. For sensitive orgs, export with `--redact-pii` first, or use a locally hosted model.

## Try it without a client

The protocol is newline-delimited JSON, so you can poke it by hand:

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"query","arguments":{"sql":"SELECT StageName, count(*) FROM Opportunity GROUP BY 1"}}}' \
  | sf-exit mcp ./export
```
