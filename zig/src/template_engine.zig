// template_engine.zig — Native Zig template engine for hyperdjango
//
// Compiles Jinja2-compatible template strings into a Zig node tree at load time,
// then renders by walking the tree and writing directly to a contiguous output buffer.
// No Python string concatenation, no Node.render() dispatch, no list allocation.
//
// Supported syntax:
//   {{ var }}                    — variable substitution
//   {{ var.attr.key }}           — dot-access chain (dict key lookup)
//   {{ var|filter }}             — filter pipe (native or Python fallback)
//   {{ var|filter(arg) }}        — filter with argument
//   {{ x + 1 }}, {{ a * b }}    — math operators (+, -, *, /, //, %, **)
//   {{ "hello" ~ name }}         — string concatenation
//   {{ x if cond else y }}       — ternary inline expression
//   {{ (a + b) * c }}            — parenthesized grouping
//   {% if condition %}...{% elif %}...{% else %}...{% endif %}
//   {% for item in items %}...{% empty %}...{% endfor %}
//   {% for k, v in dict.items() %} — tuple unpacking
//   {% include "partial.html" %} — static includes (resolved at compile time)
//   {% block name %}...{% endblock %}
//   {% extends "base.html" %}    — static inheritance (pre-resolved at compile time)
//   {# comment #}                — stripped at compile time
//
// Native filters (compiled inline):
//   escape, safe, lower, upper, title, capitalize, trim, strip,
//   length, default, join, first, last, int, float, string,
//   replace, truncate, wordcount, urlencode, striptags
//
// Python fallback filters:
//   Any filter not in native list → calls registered PyObject* callable

const std = @import("std");
pub const py = @import("py.zig");
const c = py.c;
const string_ops = @import("string_ops.zig");

const allocator = std.heap.c_allocator;

// ── Token types ──────────────────────────────────────────────────────────────

const TokenType = enum {
    text, // raw text between tags
    var_expr, // {{ ... }}
    tag, // {% ... %}
    comment, // {# ... #}
};

const Token = struct {
    type: TokenType,
    content: []const u8, // for text: raw text; for var/tag: content between delimiters (trimmed)
    trim_left: bool = false, // {%- or {{- — strip whitespace from preceding text
    trim_right: bool = false, // -%} or -}} — strip whitespace from following text
};

// ── Compiled node types ──────────────────────────────────────────────────────

const NodeType = enum {
    text, // static text → memcpy to buffer
    variable, // {{ expr }} → lookup + optional filters + write
    if_block, // {% if %} with branches
    for_block, // {% for %} with body + empty
    include, // {% include %} (pre-resolved at compile time)
    dynamic_include, // {% include var %} (resolved at render time)
    block_def, // {% block name %} (for inheritance)
    set_var, // {% set x = expr %}
    macro_def, // {% macro name(args) %}body{% endmacro %}
    macro_call, // {{ macro_name(args) }} — call a macro
    call_block, // {% call macro_name() %}body{% endcall %}
    with_block, // {% with x=1, y=2 %}body{% endwith %} — scoped variables
    super_call, // {{ super() }} — render parent block content
    break_stmt, // {% break %} — exit innermost for loop
    continue_stmt, // {% continue %} — skip to next iteration
    autoescape_block, // {% autoescape true/false %}...{% endautoescape %}
    do_stmt, // {% do expr %} — evaluate expression, discard result (side-effects)
    debug_stmt, // {% debug %} — dump context variables for debugging
    trans_block, // {% trans %}...{% endtrans %} — i18n translation
};

// Control flow signal from renderNodes back to for-loop iteration
const LoopControl = enum {
    normal, // continue rendering
    break_loop, // {% break %} — exit the for loop
    continue_loop, // {% continue %} — skip to next iteration
};

const FilterSpec = struct {
    name: []const u8,
    arg: ?[]const u8, // filter argument (string literal or null)
    native_id: i32, // >=0 for native filter, -1 for Python fallback
    py_func: ?*c.PyObject, // Python callable for fallback filters (null for native)
};

const VarPath = struct {
    // "user.name.first" → ["user", "name", "first"]. Each slice is
    // null-terminated (`[:0]const u8`) so render-time C API calls
    // (PyMapping_GetItemString, PyObject_GetAttr, etc.) can use
    // `part.ptr` directly without the per-access `allocator.dupeZ` +
    // `allocator.free` round-trip that was a measured hot path in
    // template-heavy endpoints.
    parts: [][:0]const u8,
};

const IfBranch = struct {
    condition_expr: ?*Expr, // null = else branch (always true)
    body: []CompiledNode,
};

const CompareOp = enum {
    none, // simple truthiness
    eq, // ==
    ne, // !=
    lt, // <
    gt, // >
    le, // <=
    ge, // >=
};

const MacroParam = struct {
    name: []const u8,
    default_val: ?[]const u8, // null = required, string = default value
};

const CompiledNode = struct {
    type: NodeType,
    // Fields used depend on type:
    text: []const u8, // text: static content
    var_path: VarPath, // variable: lookup path
    filters: []FilterSpec, // variable: filter chain
    if_branches: []IfBranch, // if_block: condition → body pairs
    for_var: []const u8, // for_block: loop variable name (comma-separated for unpacking)
    for_iter: VarPath, // for_block: iterable path
    for_iter_filters: []FilterSpec, // for_block: filters on iterable
    for_body: []CompiledNode, // for_block: body nodes
    for_empty: []CompiledNode, // for_block: empty clause nodes
    children: []CompiledNode, // block_def/include/macro_def: child nodes
    super_children: ?[]CompiledNode = null, // block_def: parent's content for {{ super() }}
    block_name: []const u8, // block_def/macro_def: name
    set_name: []const u8, // set_var: variable name
    macro_params: []MacroParam, // macro_def: parameter list
    macro_args: [][]const u8, // macro_call: argument expressions
    expr: ?*Expr, // variable: evaluated expression (for math/ternary/concat)
    ignore_missing: bool = false, // dynamic_include: silently skip if template not found
};

pub const CompiledTemplate = struct {
    nodes: []CompiledNode,
    // Block registry for inheritance — maps block name → node pointer (supports nested blocks)
    blocks: std.StringHashMapUnmanaged(*CompiledNode),
    // Macro registry — maps macro name → node index
    macros: std.StringHashMapUnmanaged(usize),
    // Source path for error messages
    source_path: []const u8,
    // Registered Python fallback filters
    py_filters: std.StringHashMapUnmanaged(*c.PyObject),
    // Template loader callback for imports/includes (Python callable, or null)
    loader: ?*c.PyObject = null,
    // Dynamic extends — expression evaluated at render time to get parent path
    dynamic_extends_expr: ?*Expr = null,
    // Dynamic extends — child nodes preserved for render-time block merging
    dynamic_extends_child_nodes: ?[]CompiledNode = null,

    pub fn deinit(self: *CompiledTemplate) void {
        freeNodes(self.nodes);
        self.blocks.deinit(allocator);
        self.macros.deinit(allocator);
        self.py_filters.deinit(allocator);
        if (self.loader) |l| c.Py_DecRef(l);
        if (self.dynamic_extends_expr) |e| {
            e.deinit();
            allocator.destroy(e);
        }
        if (self.dynamic_extends_child_nodes) |cn| {
            freeNodes(cn);
            allocator.free(cn);
        }
        allocator.free(self.source_path);
        allocator.free(self.nodes);
    }
};

fn freeNodes(nodes: []CompiledNode) void {
    for (nodes) |*node| {
        switch (node.type) {
            .text => allocator.free(node.text),
            .variable => {
                freeVarPath(&node.var_path);
                freeFilters(node.filters);
                if (node.expr) |e| {
                    e.deinit();
                    allocator.destroy(e);
                }
            },
            .if_block => {
                for (node.if_branches) |*branch| {
                    if (branch.condition_expr) |expr| {
                        expr.deinit();
                        allocator.destroy(expr);
                    }
                    freeNodes(branch.body);
                    allocator.free(branch.body);
                }
                allocator.free(node.if_branches);
            },
            .for_block => {
                allocator.free(node.for_var);
                freeVarPath(&node.for_iter);
                freeFilters(node.for_iter_filters);
                freeNodes(node.for_body);
                allocator.free(node.for_body);
                freeNodes(node.for_empty);
                allocator.free(node.for_empty);
            },
            .block_def, .include => {
                allocator.free(node.block_name);
                freeNodes(node.children);
                allocator.free(node.children);
                if (node.super_children) |sc| {
                    freeNodes(sc);
                    allocator.free(sc);
                }
            },
            .dynamic_include => {
                allocator.free(node.block_name);
                if (node.expr) |e| {
                    e.deinit();
                    allocator.destroy(e);
                }
                // Note: children are NOT owned by dynamic_include — they're
                // compiled on-the-fly at render time, not pre-resolved
            },
            .super_call => {}, // No owned data
            .set_var => {
                allocator.free(node.set_name);
                freeVarPath(&node.var_path);
                freeFilters(node.filters);
                if (node.expr) |e| {
                    e.deinit();
                    allocator.destroy(e);
                }
            },
            .macro_def => {
                allocator.free(node.block_name);
                for (node.macro_params) |*p| {
                    allocator.free(p.name);
                    if (p.default_val) |d| allocator.free(d);
                }
                allocator.free(node.macro_params);
                freeNodes(node.children);
                allocator.free(node.children);
            },
            .macro_call => {
                allocator.free(node.block_name);
                for (node.macro_args) |a| allocator.free(a);
                allocator.free(node.macro_args);
            },
            .call_block => {
                allocator.free(node.block_name);
                for (node.macro_args) |a| allocator.free(a);
                allocator.free(node.macro_args);
                freeNodes(node.children);
                allocator.free(node.children);
            },
            .with_block => {
                for (node.macro_params) |*p| {
                    allocator.free(p.name);
                    if (p.default_val) |d| allocator.free(d);
                }
                allocator.free(node.macro_params);
                freeNodes(node.children);
                allocator.free(node.children);
            },
            .autoescape_block => {
                freeNodes(node.children);
                allocator.free(node.children);
            },
            .break_stmt, .continue_stmt, .debug_stmt => {}, // No owned data
            .do_stmt => {
                // do_stmt has a parsed expression tree
                if (node.expr) |e| {
                    e.deinit();
                    allocator.destroy(e);
                }
            },
            .trans_block => {
                // trans_block: children = body nodes, macro_params = bindings
                freeNodes(node.children);
            },
        }
    }
}

/// Deep clone a node array — duplicates ALL allocations so the clone is independently owned.
/// Used by {% extends %} to preserve parent block content in super_children,
/// and by dynamic extends to preserve child nodes for render-time block merging.
///
/// SAFETY: Every owned allocation must be cloned. Sharing any allocation between
/// the original and clone causes double-free when both are freed via freeNodes().
fn deepCloneNodes(nodes: []const CompiledNode) []CompiledNode {
    if (nodes.len == 0) return &.{};
    const cloned = allocator.alloc(CompiledNode, nodes.len) catch return &.{};
    for (nodes, 0..) |*node, i| {
        // Copy non-pointer scalar fields (type, ignore_missing, etc.)
        cloned[i] = node.*;
        // SAFETY: Immediately zero ALL owned pointer fields to prevent
        // shared-pointer aliasing if any deep-clone allocation fails.
        // Each field is then overwritten by its deep-clone below.
        cloned[i].text = "";
        cloned[i].block_name = "";
        cloned[i].set_name = "";
        cloned[i].for_var = "";
        cloned[i].children = &.{};
        cloned[i].super_children = null;
        cloned[i].for_body = &.{};
        cloned[i].for_empty = &.{};
        cloned[i].if_branches = &.{};
        cloned[i].var_path = .{ .parts = &.{} };
        cloned[i].for_iter = .{ .parts = &.{} };
        cloned[i].filters = &.{};
        cloned[i].for_iter_filters = &.{};
        cloned[i].macro_params = &.{};
        cloned[i].macro_args = &.{};
        cloned[i].expr = null;

        // ── Clone all owned string slices ──
        cloned[i].text = if (node.text.len > 0) (allocator.dupe(u8, node.text) catch "") else "";
        cloned[i].block_name = if (node.block_name.len > 0) (allocator.dupe(u8, node.block_name) catch "") else "";
        cloned[i].set_name = if (node.set_name.len > 0) (allocator.dupe(u8, node.set_name) catch "") else "";
        cloned[i].for_var = if (node.for_var.len > 0) (allocator.dupe(u8, node.for_var) catch "") else "";

        // ── Clone child node arrays (recursive) ──
        cloned[i].children = deepCloneNodes(node.children);
        if (node.super_children) |sc| {
            cloned[i].super_children = deepCloneNodes(sc);
        }
        cloned[i].for_body = deepCloneNodes(node.for_body);
        cloned[i].for_empty = deepCloneNodes(node.for_empty);

        // ── Clone if_branches (condition exprs + body nodes) ──
        if (node.if_branches.len > 0) {
            const branches = allocator.alloc(IfBranch, node.if_branches.len) catch continue;
            for (node.if_branches, 0..) |*branch, bi| {
                branches[bi] = .{
                    .condition_expr = deepCloneExpr(branch.condition_expr),
                    .body = deepCloneNodes(branch.body),
                };
            }
            cloned[i].if_branches = branches;
        }

        // ── Clone var_path (owned parts array with owned strings) ──
        cloned[i].var_path = deepCloneVarPath(&node.var_path);

        // ── Clone for_iter VarPath ──
        cloned[i].for_iter = deepCloneVarPath(&node.for_iter);

        // ── Clone filter chains ──
        cloned[i].filters = deepCloneFilters(node.filters);
        cloned[i].for_iter_filters = deepCloneFilters(node.for_iter_filters);

        // ── Clone macro params ──
        cloned[i].macro_params = deepCloneMacroParams(node.macro_params);

        // ── Clone macro args ──
        cloned[i].macro_args = deepCloneMacroArgs(node.macro_args);

        // ── Clone expression tree ──
        cloned[i].expr = deepCloneExpr(node.expr);
    }
    return cloned;
}

/// Deep clone a VarPath — duplicates the parts array and each part string.
fn deepCloneVarPath(vp: *const VarPath) VarPath {
    if (vp.parts.len == 0) return .{ .parts = &.{} };
    const parts = allocator.alloc([:0]const u8, vp.parts.len) catch return .{ .parts = &.{} };
    for (vp.parts, 0..) |p, pi| {
        parts[pi] = allocator.dupeZ(u8, p) catch "";
    }
    return .{ .parts = parts };
}

/// Deep clone a filter chain — duplicates the FilterSpec array, each name and arg.
fn deepCloneFilters(filters: []const FilterSpec) []FilterSpec {
    if (filters.len == 0) return &.{};
    const cloned = allocator.alloc(FilterSpec, filters.len) catch return &.{};
    for (filters, 0..) |*f, fi| {
        cloned[fi] = f.*;
        cloned[fi].name = allocator.dupe(u8, f.name) catch "";
        cloned[fi].arg = if (f.arg) |a| (allocator.dupe(u8, a) catch null) else null;
        // py_func: shared Python object ref — not owned by us, don't clone
    }
    return cloned;
}

/// Deep clone an Expr tree (recursive). Returns null if input is null.
fn deepCloneExpr(expr: ?*const Expr) ?*Expr {
    const e = expr orelse return null;
    const cloned = allocator.create(Expr) catch return null;
    cloned.* = e.*;
    // Clone owned string
    cloned.str_val = if (e.str_val.len > 0 and e.type != .literal_var)
        (allocator.dupe(u8, e.str_val) catch "")
    else
        e.str_val; // literal_var str_val is "" or alias into var_path — safe as empty
    // Clone var_path
    cloned.var_path = deepCloneVarPath(&e.var_path);
    // Recursively clone sub-expressions
    cloned.left = deepCloneExpr(e.left);
    cloned.right = deepCloneExpr(e.right);
    cloned.ternary_false = deepCloneExpr(e.ternary_false);
    // Clone call_args
    if (e.call_args) |args| {
        const cloned_args = allocator.alloc(*Expr, args.len) catch null;
        if (cloned_args) |ca| {
            for (args, 0..) |a, ai| {
                ca[ai] = deepCloneExpr(a) orelse continue;
            }
            cloned.call_args = ca;
        }
    }
    return cloned;
}

/// Deep clone a MacroParam array — duplicates names and default values.
fn deepCloneMacroParams(params: []const MacroParam) []MacroParam {
    if (params.len == 0) return &.{};
    var cloned = allocator.alloc(MacroParam, params.len) catch return &.{};
    for (params, 0..) |*p, pi| {
        cloned[pi].name = allocator.dupe(u8, p.name) catch "";
        cloned[pi].default_val = if (p.default_val) |d| (allocator.dupe(u8, d) catch null) else null;
    }
    return cloned;
}

/// Deep clone a macro args array — duplicates each string.
fn deepCloneMacroArgs(args: []const []const u8) [][]const u8 {
    if (args.len == 0) return &.{};
    var cloned = allocator.alloc([]const u8, args.len) catch return &.{};
    for (args, 0..) |a, ai| {
        cloned[ai] = allocator.dupe(u8, a) catch "";
    }
    return cloned;
}

fn freeVarPath(vp: *VarPath) void {
    for (vp.parts) |p| allocator.free(p);
    allocator.free(vp.parts);
}

fn freeFilters(filters: []FilterSpec) void {
    for (filters) |*f| {
        allocator.free(f.name);
        if (f.arg) |a| allocator.free(a);
    }
    allocator.free(filters);
}

// ── Lexer ────────────────────────────────────────────────────────────────────

fn tokenize(source: []const u8) !std.ArrayListUnmanaged(Token) {
    var tokens: std.ArrayListUnmanaged(Token) = .empty;
    var pos: usize = 0;

    const d = custom_delimiters orelse DEFAULT_DELIMS;

    while (pos < source.len) {
        // Look for next tag opener using configured delimiters
        var next_tag: ?usize = null;
        var tag_type: TokenType = .text;
        var open_len: usize = 2;

        var scan = pos;
        while (scan < source.len) : (scan += 1) {
            // Check variable start (e.g., "{{")
            if (scan + d.var_start.len <= source.len and std.mem.eql(u8, source[scan..][0..d.var_start.len], d.var_start)) {
                next_tag = scan;
                tag_type = .var_expr;
                open_len = d.var_start.len;
                break;
            }
            // Check block start (e.g., "{%")
            if (scan + d.block_start.len <= source.len and std.mem.eql(u8, source[scan..][0..d.block_start.len], d.block_start)) {
                next_tag = scan;
                tag_type = .tag;
                open_len = d.block_start.len;
                break;
            }
            // Check comment start (e.g., "{#")
            if (scan + d.comment_start.len <= source.len and std.mem.eql(u8, source[scan..][0..d.comment_start.len], d.comment_start)) {
                next_tag = scan;
                tag_type = .comment;
                open_len = d.comment_start.len;
                break;
            }
        }

        if (next_tag) |tag_start| {
            // Emit text before this tag
            if (tag_start > pos) {
                try tokens.append(allocator, .{
                    .type = .text,
                    .content = try allocator.dupe(u8, source[pos..tag_start]),
                });
            }

            // Find closing delimiter
            const close_delim: []const u8 = switch (tag_type) {
                .var_expr => d.var_end,
                .tag => d.block_end,
                .comment => d.comment_end,
                .text => unreachable,
            };
            var content_start = tag_start + open_len;

            // Check for trim-left marker: {{- or {%- or {#-
            var trim_left = false;
            if (content_start < source.len and source[content_start] == '-') {
                trim_left = true;
                content_start += 1;
            }

            const close_pos = std.mem.indexOf(u8, source[content_start..], close_delim) orelse {
                try tokens.append(allocator, .{
                    .type = .text,
                    .content = try allocator.dupe(u8, source[pos..]),
                });
                return tokens;
            };
            var content_end = content_start + close_pos;

            // Check for trim-right marker: -}} or -%} or -#}
            var trim_right = false;
            if (content_end > content_start and source[content_end - 1] == '-') {
                trim_right = true;
                content_end -= 1;
            }

            const content = std.mem.trim(u8, source[content_start..content_end], " \t\n\r");

            // Handle {% raw %} blocks — pass content through as literal text
            if (tag_type == .tag and std.mem.eql(u8, content, "raw")) {
                // Build endraw pattern: e.g., "{% endraw %}" with configured delimiters
                var endraw_buf: [64]u8 = undefined;
                const endraw_len = d.block_start.len + " endraw ".len + d.block_end.len;
                if (endraw_len <= endraw_buf.len) {
                    var ep: usize = 0;
                    @memcpy(endraw_buf[ep..][0..d.block_start.len], d.block_start);
                    ep += d.block_start.len;
                    @memcpy(endraw_buf[ep..][0..8], " endraw ");
                    ep += 8;
                    @memcpy(endraw_buf[ep..][0..d.block_end.len], d.block_end);
                    ep += d.block_end.len;
                    const endraw_pattern = endraw_buf[0..ep];
                    const raw_end = std.mem.indexOf(u8, source[content_start + close_pos + close_delim.len ..], endraw_pattern);
                    if (raw_end) |re| {
                        const raw_start = content_start + close_pos + close_delim.len;
                        try tokens.append(allocator, .{
                            .type = .text,
                            .content = try allocator.dupe(u8, source[raw_start .. raw_start + re]),
                        });
                        pos = raw_start + re + endraw_pattern.len;
                        continue;
                    }
                }
            }

            if (tag_type != .comment) {
                // If trim_left, strip trailing whitespace from previous text token
                if (trim_left and tokens.items.len > 0) {
                    const last = &tokens.items[tokens.items.len - 1];
                    if (last.type == .text) {
                        const trimmed = std.mem.trimEnd(u8, last.content, " \t\n\r");
                        if (trimmed.len < last.content.len) {
                            const old = last.content;
                            last.content = allocator.dupe(u8, trimmed) catch last.content;
                            allocator.free(old);
                        }
                    }
                }

                try tokens.append(allocator, .{
                    .type = tag_type,
                    .content = try allocator.dupe(u8, content),
                    .trim_left = trim_left,
                    .trim_right = trim_right,
                });
            }

            // Move past the closing delimiter
            pos = content_start + close_pos + close_delim.len;

            // If trim_right, strip leading whitespace from next text
            // (handled at emit time: next text token gets trimmed when appended)
            if (trim_right and pos < source.len) {
                // Skip whitespace after closing delimiter
                while (pos < source.len and (source[pos] == ' ' or source[pos] == '\t' or source[pos] == '\n' or source[pos] == '\r')) {
                    pos += 1;
                }
            }
        } else {
            // No more tags — rest is text
            if (pos < source.len) {
                try tokens.append(allocator, .{
                    .type = .text,
                    .content = try allocator.dupe(u8, source[pos..]),
                });
            }
            break;
        }
    }

    return tokens;
}

// ── Parser ───────────────────────────────────────────────────────────────────

fn parseVarExpr(expr: []const u8) ParseError!struct { path: VarPath, filters: []FilterSpec } {
    // Split on | for filters: "user.name|lower|default('anon')"
    var parts_list: std.ArrayListUnmanaged([]const u8) = .empty;
    defer parts_list.deinit(allocator);

    // Simple pipe split (doesn't handle pipes inside quotes)
    var start: usize = 0;
    var in_quotes = false;
    for (expr, 0..) |ch, i| {
        if (ch == '\'' or ch == '"') in_quotes = !in_quotes;
        if (ch == '|' and !in_quotes) {
            try parts_list.append(allocator, std.mem.trim(u8, expr[start..i], " "));
            start = i + 1;
        }
    }
    try parts_list.append(allocator, std.mem.trim(u8, expr[start..], " "));

    // First part is the variable path
    const var_str = parts_list.items[0];
    const path = try parseVarPath(var_str);

    // Remaining parts are filters
    var filters: std.ArrayListUnmanaged(FilterSpec) = .empty;
    for (parts_list.items[1..]) |filter_str| {
        try filters.append(allocator, try parseFilter(filter_str));
    }

    return .{
        .path = path,
        .filters = try filters.toOwnedSlice(allocator),
    };
}

// ── Expression Tokenizer ─────────────────────────────────────────────────────
// Proper tokenizer for expression content. Handles all spacing variations,
// multi-char operators, string literals, and numbers as first-class tokens.

const ExprTokenType = enum {
    name, // identifier: x, user, items
    integer_lit, // 42, 100
    float_lit, // 3.14, 1.0
    string_lit, // "hello", 'world' (includes quotes in value)
    plus, // +
    minus, // -
    star, // *
    double_star, // **
    slash, // /
    double_slash, // //
    percent, // %
    tilde, // ~
    lparen, // (
    rparen, // )
    lbracket, // [
    rbracket, // ]
    lbrace, // {
    rbrace, // }
    colon, // :
    assign, // = (single equals — for keyword args)
    dot, // .
    pipe, // |
    comma, // ,
    eq, // ==
    ne, // !=
    lt, // <
    gt, // >
    le, // <=
    ge, // >=
    kw_and,
    kw_or,
    kw_not,
    kw_in,
    kw_is,
    kw_if,
    kw_else,
    kw_true,
    kw_false,
    kw_none,
    eof,
};

const ExprToken = struct {
    type: ExprTokenType,
    value: []const u8, // slice into original source — zero allocation
};

const ExprParser = struct {
    tokens: []const ExprToken,
    pos: usize,

    fn current(self: *const ExprParser) ExprToken {
        if (self.pos < self.tokens.len) return self.tokens[self.pos];
        return .{ .type = .eof, .value = "" };
    }

    fn advance(self: *ExprParser) ExprToken {
        const tok = self.current();
        if (self.pos < self.tokens.len) self.pos += 1;
        return tok;
    }

    fn check(self: *const ExprParser, t: ExprTokenType) bool {
        return self.current().type == t;
    }

    fn match(self: *ExprParser, t: ExprTokenType) bool {
        if (self.check(t)) {
            _ = self.advance();
            return true;
        }
        return false;
    }

    fn expect(self: *ExprParser, t: ExprTokenType) ParseError!ExprToken {
        if (self.check(t)) return self.advance();
        return error.UnexpectedToken;
    }

    fn checkAny(self: *const ExprParser, types: []const ExprTokenType) bool {
        const cur = self.current().type;
        for (types) |t| {
            if (cur == t) return true;
        }
        return false;
    }
};

fn isNameStart(ch: u8) bool {
    return (ch >= 'a' and ch <= 'z') or (ch >= 'A' and ch <= 'Z') or ch == '_';
}

fn isNameChar(ch: u8) bool {
    return isNameStart(ch) or (ch >= '0' and ch <= '9');
}

fn classifyKeyword(word: []const u8) ExprTokenType {
    if (std.mem.eql(u8, word, "and")) return .kw_and;
    if (std.mem.eql(u8, word, "or")) return .kw_or;
    if (std.mem.eql(u8, word, "not")) return .kw_not;
    if (std.mem.eql(u8, word, "in")) return .kw_in;
    if (std.mem.eql(u8, word, "is")) return .kw_is;
    if (std.mem.eql(u8, word, "if")) return .kw_if;
    if (std.mem.eql(u8, word, "else")) return .kw_else;
    if (std.mem.eql(u8, word, "True") or std.mem.eql(u8, word, "true")) return .kw_true;
    if (std.mem.eql(u8, word, "False") or std.mem.eql(u8, word, "false")) return .kw_false;
    if (std.mem.eql(u8, word, "None") or std.mem.eql(u8, word, "none")) return .kw_none;
    return .name;
}

fn tokenizeExpr(input: []const u8) ParseError!std.ArrayListUnmanaged(ExprToken) {
    var tokens: std.ArrayListUnmanaged(ExprToken) = .empty;
    var pos: usize = 0;

    while (pos < input.len) {
        const ch = input[pos];

        // 1. Skip whitespace
        if (ch == ' ' or ch == '\t' or ch == '\n' or ch == '\r') {
            pos += 1;
            continue;
        }

        // 2. String literals
        if (ch == '\'' or ch == '"') {
            const start = pos;
            pos += 1;
            while (pos < input.len and input[pos] != ch) {
                if (input[pos] == '\\' and pos + 1 < input.len) pos += 1;
                pos += 1;
            }
            if (pos < input.len) pos += 1; // closing quote
            try tokens.append(allocator, .{ .type = .string_lit, .value = input[start..pos] });
            continue;
        }

        // 3. Numbers
        if (ch >= '0' and ch <= '9') {
            const start = pos;
            while (pos < input.len and input[pos] >= '0' and input[pos] <= '9') pos += 1;
            if (pos < input.len and input[pos] == '.' and pos + 1 < input.len and input[pos + 1] >= '0' and input[pos + 1] <= '9') {
                pos += 1;
                while (pos < input.len and input[pos] >= '0' and input[pos] <= '9') pos += 1;
                try tokens.append(allocator, .{ .type = .float_lit, .value = input[start..pos] });
            } else {
                try tokens.append(allocator, .{ .type = .integer_lit, .value = input[start..pos] });
            }
            continue;
        }

        // 4. Multi-char operators (longest match first)
        if (pos + 1 < input.len) {
            const two = input[pos .. pos + 2];
            const tt: ?ExprTokenType = if (std.mem.eql(u8, two, "**")) .double_star else if (std.mem.eql(u8, two, "//")) .double_slash else if (std.mem.eql(u8, two, "==")) .eq else if (std.mem.eql(u8, two, "!=")) .ne else if (std.mem.eql(u8, two, "<=")) .le else if (std.mem.eql(u8, two, ">=")) .ge else null;
            if (tt) |t| {
                try tokens.append(allocator, .{ .type = t, .value = two });
                pos += 2;
                continue;
            }
        }

        // 5. Single-char operators
        const single_type: ?ExprTokenType = switch (ch) {
            '+' => .plus,
            '-' => .minus,
            '*' => .star,
            '/' => .slash,
            '%' => .percent,
            '~' => .tilde,
            '(' => .lparen,
            ')' => .rparen,
            '[' => .lbracket,
            ']' => .rbracket,
            '{' => .lbrace,
            '}' => .rbrace,
            '.' => .dot,
            '|' => .pipe,
            ',' => .comma,
            ':' => .colon,
            '=' => .assign,
            '<' => .lt,
            '>' => .gt,
            else => null,
        };
        if (single_type) |st| {
            try tokens.append(allocator, .{ .type = st, .value = input[pos .. pos + 1] });
            pos += 1;
            continue;
        }

        // 6. Names / keywords
        if (isNameStart(ch)) {
            const start = pos;
            while (pos < input.len and isNameChar(input[pos])) pos += 1;
            const word = input[start..pos];
            try tokens.append(allocator, .{ .type = classifyKeyword(word), .value = word });
            continue;
        }

        // Unknown — skip
        pos += 1;
    }

    return tokens;
}

// ── Unified expression entry point ──────────────────────────────────────────

/// Parse an expression string into an Expr tree with optional filter chain.
/// Used for {{ expr }}, {% if expr %}, {% set x = expr %}.
fn parseExpressionFull(content: []const u8) ParseError!struct { expr: *Expr, filters: []FilterSpec } {
    var tokens = try tokenizeExpr(content);
    defer tokens.deinit(allocator);

    var p = ExprParser{ .tokens = tokens.items, .pos = 0 };
    const expr = try exprParseTernary(&p);

    // Extract trailing filter chain: expr|filter1|filter2(arg)
    // Filters are stored as postfix nodes — peel them off
    var filters: std.ArrayListUnmanaged(FilterSpec) = .empty;
    var current_expr = expr;
    // Walk down left spine of filter_expr nodes to extract filter chain
    // We need to reverse since the outermost filter_expr is the last applied
    var filter_stack: std.ArrayListUnmanaged(FilterSpec) = .empty;
    defer filter_stack.deinit(allocator);
    while (current_expr.type == .filter_expr) {
        // If this filter has multi-args (call_args), stop extraction — leave in expr tree
        // so evalExpr can handle it with properly tokenized arguments
        if (current_expr.call_args != null) break;

        try filter_stack.append(allocator, .{
            .name = try allocator.dupe(u8, current_expr.str_val),
            .arg = if (current_expr.right) |arg_expr| blk: {
                if (arg_expr.type == .literal_str) {
                    break :blk try allocator.dupe(u8, arg_expr.str_val);
                } else if (arg_expr.type == .literal_int) {
                    var buf: [24]u8 = undefined;
                    const s = std.fmt.bufPrint(&buf, "{d}", .{arg_expr.int_val}) catch break :blk null;
                    break :blk try allocator.dupe(u8, s);
                }
                break :blk null;
            } else null,
            .native_id = getNativeFilterId(current_expr.str_val),
            .py_func = null,
        });
        const inner = current_expr.left.?;
        if (current_expr.str_val.len > 0) allocator.free(current_expr.str_val);
        if (current_expr.right) |r| {
            r.deinit();
            allocator.destroy(r);
        }
        current_expr.left = null;
        current_expr.right = null;
        allocator.destroy(current_expr);
        current_expr = inner;
    }
    // Reverse the filter stack (outermost → innermost becomes left-to-right)
    var fi: usize = filter_stack.items.len;
    while (fi > 0) {
        fi -= 1;
        try filters.append(allocator, filter_stack.items[fi]);
    }

    return .{
        .expr = current_expr,
        .filters = try filters.toOwnedSlice(allocator),
    };
}

fn parseVarPath(var_str: []const u8) ParseError!VarPath {
    // Handle string literals
    if (var_str.len >= 2 and (var_str[0] == '\'' or var_str[0] == '"') and var_str[var_str.len - 1] == var_str[0]) {
        // String literal — store as single-part path with quote marker
        var parts = try allocator.alloc([:0]const u8, 1);
        parts[0] = try allocator.dupeZ(u8, var_str);
        return .{ .parts = parts };
    }

    // Handle numeric literals (int or float) — don't split on '.'
    if (var_str.len > 0 and (var_str[0] >= '0' and var_str[0] <= '9' or var_str[0] == '-')) {
        if (std.fmt.parseFloat(f64, var_str)) |_| {
            var parts = try allocator.alloc([:0]const u8, 1);
            parts[0] = try allocator.dupeZ(u8, var_str);
            return .{ .parts = parts };
        } else |_| {}
    }

    var parts: std.ArrayListUnmanaged([:0]const u8) = .empty;
    var it = std.mem.splitScalar(u8, var_str, '.');
    while (it.next()) |part| {
        if (part.len > 0) {
            try parts.append(allocator, try allocator.dupeZ(u8, part));
        }
    }
    return .{ .parts = try parts.toOwnedSlice(allocator) };
}

fn parseFilter(filter_str: []const u8) ParseError!FilterSpec {
    // "default('fallback')" → name="default", arg="fallback"
    const paren = std.mem.indexOf(u8, filter_str, "(");
    const name = if (paren) |p| filter_str[0..p] else filter_str;

    var arg: ?[]const u8 = null;
    if (paren) |p| {
        const close = std.mem.lastIndexOf(u8, filter_str, ")") orelse filter_str.len;
        var raw_arg = filter_str[p + 1 .. close];
        // Strip quotes from single argument, but preserve raw for multi-arg filters
        if (raw_arg.len >= 2 and (raw_arg[0] == '\'' or raw_arg[0] == '"')) {
            // Check if this is a single arg (no unquoted comma)
            const quote = raw_arg[0];
            const end_quote = std.mem.indexOfPos(u8, raw_arg, 1, &.{quote});
            if (end_quote) |eq| {
                // Check if there's more after the closing quote (multi-arg)
                const after = std.mem.trim(u8, raw_arg[eq + 1 ..], " ");
                if (after.len == 0) {
                    // Single quoted arg — strip quotes
                    raw_arg = raw_arg[1..eq];
                }
                // else: multi-arg like 'a', 'b' — keep raw for handler to parse
            } else {
                raw_arg = raw_arg[1 .. raw_arg.len - 1];
            }
        }
        arg = try allocator.dupe(u8, raw_arg);
    }

    return .{
        .name = try allocator.dupe(u8, name),
        .arg = arg,
        .native_id = getNativeFilterId(name),
        .py_func = null,
    };
}

// ── Native filter registry ───────────────────────────────────────────────────

const FILTER_ESCAPE: i32 = 0;
const FILTER_SAFE: i32 = 1;
const FILTER_LOWER: i32 = 2;
const FILTER_UPPER: i32 = 3;
const FILTER_TITLE: i32 = 4;
const FILTER_CAPITALIZE: i32 = 5;
const FILTER_TRIM: i32 = 6;
const FILTER_LENGTH: i32 = 7;
const FILTER_DEFAULT: i32 = 8;
const FILTER_JOIN: i32 = 9;
const FILTER_FIRST: i32 = 10;
const FILTER_LAST: i32 = 11;
const FILTER_INT: i32 = 12;
const FILTER_FLOAT: i32 = 13;
const FILTER_STRING: i32 = 14;
const FILTER_REPLACE: i32 = 15;
const FILTER_TRUNCATE: i32 = 16;
const FILTER_WORDCOUNT: i32 = 17;
const FILTER_URLENCODE: i32 = 18;
const FILTER_STRIPTAGS: i32 = 19;
const FILTER_ABS: i32 = 20;
const FILTER_ROUND: i32 = 21;
const FILTER_SORT: i32 = 22;
const FILTER_REVERSE: i32 = 23;
const FILTER_UNIQUE: i32 = 24;
const FILTER_TOJSON: i32 = 25;
const FILTER_LIST: i32 = 26;
const FILTER_BOOL: i32 = 27;
const FILTER_BATCH: i32 = 28;
const FILTER_SUM: i32 = 29;
const FILTER_MIN: i32 = 30;
const FILTER_MAX: i32 = 31;
const FILTER_MAP: i32 = 32;
const FILTER_INDENT: i32 = 33;
const FILTER_CENTER: i32 = 34;
const FILTER_DICTSORT: i32 = 35;
const FILTER_ITEMS: i32 = 36;
const FILTER_COUNT: i32 = 37;
const FILTER_GROUPBY: i32 = 38;
const FILTER_FILESIZEFORMAT: i32 = 39;
const FILTER_WORDWRAP: i32 = 40;
const FILTER_FORMAT: i32 = 41;
const FILTER_URLIZE: i32 = 42;
const FILTER_SELECT: i32 = 43;
const FILTER_REJECT: i32 = 44;
const FILTER_ATTR: i32 = 45;
const FILTER_XMLATTR: i32 = 46;

fn getNativeFilterId(name: []const u8) i32 {
    const map = .{
        .{ "escape", FILTER_ESCAPE },         .{ "e", FILTER_ESCAPE },
        .{ "safe", FILTER_SAFE },             .{ "lower", FILTER_LOWER },
        .{ "upper", FILTER_UPPER },           .{ "title", FILTER_TITLE },
        .{ "capitalize", FILTER_CAPITALIZE }, .{ "trim", FILTER_TRIM },
        .{ "strip", FILTER_TRIM },            .{ "length", FILTER_LENGTH },
        .{ "default", FILTER_DEFAULT },       .{ "join", FILTER_JOIN },
        .{ "first", FILTER_FIRST },           .{ "last", FILTER_LAST },
        .{ "int", FILTER_INT },               .{ "float", FILTER_FLOAT },
        .{ "string", FILTER_STRING },         .{ "replace", FILTER_REPLACE },
        .{ "truncate", FILTER_TRUNCATE },     .{ "wordcount", FILTER_WORDCOUNT },
        .{ "urlencode", FILTER_URLENCODE },   .{ "striptags", FILTER_STRIPTAGS },
        .{ "abs", FILTER_ABS },               .{ "round", FILTER_ROUND },
        .{ "sort", FILTER_SORT },             .{ "reverse", FILTER_REVERSE },
        .{ "unique", FILTER_UNIQUE },         .{ "tojson", FILTER_TOJSON },
        .{ "list", FILTER_LIST },             .{ "bool", FILTER_BOOL },
        .{ "batch", FILTER_BATCH },           .{ "sum", FILTER_SUM },
        .{ "min", FILTER_MIN },               .{ "max", FILTER_MAX },
        .{ "map", FILTER_MAP },               .{ "indent", FILTER_INDENT },
        .{ "center", FILTER_CENTER },         .{ "dictsort", FILTER_DICTSORT },
        .{ "items", FILTER_ITEMS },           .{ "count", FILTER_COUNT },
        .{ "groupby", FILTER_GROUPBY },       .{ "filesizeformat", FILTER_FILESIZEFORMAT },
        .{ "wordwrap", FILTER_WORDWRAP },     .{ "format", FILTER_FORMAT },
        .{ "urlize", FILTER_URLIZE },         .{ "select", FILTER_SELECT },
        .{ "reject", FILTER_REJECT },         .{ "attr", FILTER_ATTR },
        .{ "xmlattr", FILTER_XMLATTR },
    };
    inline for (map) |entry| {
        if (std.mem.eql(u8, name, entry[0])) return entry[1];
    }
    return -1; // Python fallback
}

// ── Node parsing (recursive descent) ─────────────────────────────────────────

const ParseError = error{ OutOfMemory, BadForSyntax, BadSetSyntax, BadSyntax, UnexpectedToken };

// ── Compile-time directive storage (set during parseNodes, read in compile()) ──

const ImportDirective = struct {
    path: []const u8, // "macros.html"
    alias: []const u8, // "m" (for {% import "macros.html" as m %})
};

const FromImportDirective = struct {
    path: []const u8, // "macros.html"
    name: []const u8, // "input_field" (specific macro name)
};

const IncludeDirective = struct {
    path: []const u8, // "partial.html"
    node_index: usize, // index into the nodes array for replacement
};

// Thread-local storage for extends/import directives found during parsing.
// These are set by parseNodes and consumed by compile().
threadlocal var extends_parent_path: ?[]const u8 = null;
threadlocal var extends_dynamic_expr: ?*Expr = null; // {% extends variable %} — resolved at render time
threadlocal var import_directives: std.ArrayListUnmanaged(ImportDirective) = .empty;
threadlocal var from_import_directives: std.ArrayListUnmanaged(FromImportDirective) = .empty;
threadlocal var include_directives: std.ArrayListUnmanaged(IncludeDirective) = .empty;
// Thread-local loader callback — set by Python before compile
threadlocal var template_loader: ?*c.PyObject = null;

fn parseNodes(tokens: []const Token, start: *usize, end_tags: []const []const u8) ParseError![]CompiledNode {
    var nodes: std.ArrayListUnmanaged(CompiledNode) = .empty;

    while (start.* < tokens.len) {
        const tok = tokens[start.*];

        switch (tok.type) {
            .text => {
                try nodes.append(allocator, .{
                    .type = .text,
                    .text = try allocator.dupe(u8, tok.content),
                    .var_path = .{ .parts = &.{} },
                    .filters = &.{},
                    .if_branches = &.{},
                    .for_var = "",
                    .for_iter = .{ .parts = &.{} },
                    .for_iter_filters = &.{},
                    .for_body = &.{},
                    .for_empty = &.{},
                    .children = &.{},
                    .block_name = "",
                    .set_name = "",
                    .macro_params = &.{},
                    .macro_args = &.{},
                    .expr = null,
                });
                start.* += 1;
            },
            .var_expr => {
                // Check for {{ super() }} — emit super_call node directly
                const trimmed = std.mem.trim(u8, tok.content, " ");
                if (std.mem.eql(u8, trimmed, "super()")) {
                    try nodes.append(allocator, .{
                        .type = .super_call,
                        .text = "",
                        .var_path = .{ .parts = &.{} },
                        .filters = &.{},
                        .if_branches = &.{},
                        .for_var = "",
                        .for_iter = .{ .parts = &.{} },
                        .for_iter_filters = &.{},
                        .for_body = &.{},
                        .for_empty = &.{},
                        .children = &.{},
                        .block_name = "",
                        .set_name = "",
                        .macro_params = &.{},
                        .macro_args = &.{},
                        .expr = null,
                    });
                    start.* += 1;
                    continue;
                }

                // Unified path: all expressions go through tokenizer + parser
                const parsed = try parseExpressionFull(tok.content);
                // For simple literal_var, use resolveVar path (no expr needed)
                // This avoids double-ownership of var_path
                if (parsed.expr.type == .literal_var and parsed.filters.len == 0) {
                    // Simple variable with no filters from expression parser
                    // But check if parseExpressionFull extracted filters
                }
                const is_simple_var = parsed.expr.type == .literal_var;
                try nodes.append(allocator, .{
                    .type = .variable,
                    .text = "",
                    .var_path = if (is_simple_var) parsed.expr.var_path else VarPath{ .parts = &.{} },
                    .filters = parsed.filters,
                    .if_branches = &.{},
                    .for_var = "",
                    .for_iter = .{ .parts = &.{} },
                    .for_iter_filters = &.{},
                    .for_body = &.{},
                    .for_empty = &.{},
                    .children = &.{},
                    .block_name = "",
                    .set_name = "",
                    .macro_params = &.{},
                    .macro_args = &.{},
                    .expr = if (is_simple_var) null else parsed.expr,
                });
                // If simple var, free the Expr shell but NOT the var_path (now owned by node)
                if (is_simple_var) {
                    parsed.expr.var_path = .{ .parts = &.{} }; // clear so deinit doesn't free
                    parsed.expr.deinit();
                    allocator.destroy(parsed.expr);
                }
                start.* += 1;
            },
            .tag => {
                // Check for end tags (prefix match for elif which has conditions)
                for (end_tags) |et| {
                    if (std.mem.eql(u8, tok.content, et) or
                        (std.mem.startsWith(u8, tok.content, et) and
                            tok.content.len > et.len and tok.content[et.len] == ' '))
                    {
                        return nodes.toOwnedSlice(allocator);
                    }
                }

                if (std.mem.startsWith(u8, tok.content, "if ") or std.mem.eql(u8, tok.content, "if")) {
                    const if_node = try parseIfBlock(tokens, start);
                    try nodes.append(allocator, if_node);
                } else if (std.mem.startsWith(u8, tok.content, "for ")) {
                    const for_node = try parseForBlock(tokens, start);
                    try nodes.append(allocator, for_node);
                } else if (std.mem.startsWith(u8, tok.content, "block ")) {
                    const block_node = try parseBlockDef(tokens, start);
                    try nodes.append(allocator, block_node);
                } else if (std.mem.startsWith(u8, tok.content, "set ")) {
                    try nodes.append(allocator, try parseSetVar(tok.content));
                    start.* += 1;
                } else if (std.mem.startsWith(u8, tok.content, "macro ")) {
                    const macro_node = try parseMacroDef(tokens, start);
                    try nodes.append(allocator, macro_node);
                } else if (std.mem.startsWith(u8, tok.content, "call ") or std.mem.startsWith(u8, tok.content, "call(")) {
                    const call_node = try parseCallBlock(tokens, start);
                    try nodes.append(allocator, call_node);
                } else if (std.mem.startsWith(u8, tok.content, "with ") or std.mem.eql(u8, tok.content, "with")) {
                    const with_node = try parseWithBlock(tokens, start);
                    try nodes.append(allocator, with_node);
                } else if (std.mem.startsWith(u8, tok.content, "static ") or
                    std.mem.startsWith(u8, tok.content, "url ") or
                    std.mem.eql(u8, tok.content, "csrf_token"))
                {
                    // Django output tags: {% static 'path' %}, {% url 'name' %}, {% csrf_token %}
                    // Parse as a variable node with a filter applied
                    const tag_node = try parseDjangoOutputTag(tok.content);
                    try nodes.append(allocator, tag_node);
                    start.* += 1;
                } else if (std.mem.startsWith(u8, tok.content, "extends ")) {
                    // {% extends "base.html" %} — static: compile-time resolution
                    // {% extends layout_var %} — dynamic: render-time resolution
                    const path_raw = std.mem.trim(u8, tok.content[8..], " ");
                    if (path_raw.len >= 2 and (path_raw[0] == '\'' or path_raw[0] == '"')) {
                        // Static string literal — existing behavior
                        extends_parent_path = try allocator.dupe(u8, path_raw[1 .. path_raw.len - 1]);
                    } else if (path_raw.len > 0) {
                        // Dynamic variable — parse as expression, store for render-time resolution
                        extends_dynamic_expr = try parseExpr(path_raw);
                    }
                    start.* += 1;
                } else if (std.mem.startsWith(u8, tok.content, "import ")) {
                    // {% import "macros.html" as m %} — store import for compile-time resolution
                    const import_content = std.mem.trim(u8, tok.content[7..], " ");
                    // Parse: "path" as name
                    if (import_content.len > 4 and (import_content[0] == '\'' or import_content[0] == '"')) {
                        const quote = import_content[0];
                        const end_quote = std.mem.indexOfPos(u8, import_content, 1, &.{quote});
                        if (end_quote) |eq| {
                            const path = import_content[1..eq];
                            const rest = std.mem.trim(u8, import_content[eq + 1 ..], " ");
                            if (std.mem.startsWith(u8, rest, "as ")) {
                                const alias = std.mem.trim(u8, rest[3..], " ");
                                try import_directives.append(allocator, .{
                                    .path = try allocator.dupe(u8, path),
                                    .alias = try allocator.dupe(u8, alias),
                                });
                            }
                        }
                    }
                    start.* += 1;
                } else if (std.mem.startsWith(u8, tok.content, "from ")) {
                    // {% from "macros.html" import input_field, form_row %}
                    const from_content = std.mem.trim(u8, tok.content[5..], " ");
                    if (from_content.len > 4 and (from_content[0] == '\'' or from_content[0] == '"')) {
                        const quote = from_content[0];
                        const end_quote = std.mem.indexOfPos(u8, from_content, 1, &.{quote});
                        if (end_quote) |eq| {
                            const path = from_content[1..eq];
                            const rest = std.mem.trim(u8, from_content[eq + 1 ..], " ");
                            if (std.mem.startsWith(u8, rest, "import ")) {
                                const names_str = std.mem.trim(u8, rest[7..], " ");
                                var name_iter = std.mem.splitScalar(u8, names_str, ',');
                                while (name_iter.next()) |name_raw| {
                                    const name = std.mem.trim(u8, name_raw, " ");
                                    if (name.len > 0) {
                                        try from_import_directives.append(allocator, .{
                                            .path = try allocator.dupe(u8, path),
                                            .name = try allocator.dupe(u8, name),
                                        });
                                    }
                                }
                            }
                        }
                    }
                    start.* += 1;
                } else if (std.mem.startsWith(u8, tok.content, "include ")) {
                    // {% include "partial.html" %} — static, resolved at compile-time
                    // {% include partial_var %} — dynamic, resolved at render-time
                    // {% include ["a.html", "b.html"] ignore missing %} — fallback list
                    // {% include "partial.html" with x=expr, y=expr %} — variable bindings
                    var include_raw = std.mem.trim(u8, tok.content[8..], " ");

                    // Check for "ignore missing" suffix
                    const has_ignore_missing = std.mem.endsWith(u8, include_raw, "ignore missing");
                    if (has_ignore_missing)
                        include_raw = std.mem.trim(u8, include_raw[0 .. include_raw.len - 14], " ");

                    // Check for "with context" / "without context" suffix
                    // Also parse "with x=expr, y=expr" variable bindings
                    var without_context = false;
                    var include_bindings: []MacroParam = &.{};
                    if (std.mem.endsWith(u8, include_raw, "without context")) {
                        without_context = true;
                        include_raw = std.mem.trim(u8, include_raw[0 .. include_raw.len - 15], " ");
                    } else if (std.mem.endsWith(u8, include_raw, "with context")) {
                        // Explicit "with context" — same as default behavior
                        include_raw = std.mem.trim(u8, include_raw[0 .. include_raw.len - 12], " ");
                    } else {
                        // Check for "with key=expr, ..." variable bindings
                        // Find " with " after the template path (not inside quotes)
                        include_bindings = try parseIncludeBindings(&include_raw);
                    }

                    const include_content = include_raw;

                    // set_name="without" signals render to use empty context
                    const ctx_mode: []const u8 = if (without_context) "without" else "";

                    if (include_content.len >= 2 and (include_content[0] == '\'' or include_content[0] == '"')) {
                        // Static string literal — existing behavior
                        const quote = include_content[0];
                        const end_quote = std.mem.indexOfPos(u8, include_content, 1, &.{quote});
                        if (end_quote) |eq| {
                            const include_path = try allocator.dupe(u8, include_content[1..eq]);
                            const node = CompiledNode{
                                .type = .include,
                                .text = "",
                                .block_name = include_path,
                                .children = &.{},
                                .filters = &.{},
                                .if_branches = &.{},
                                .for_body = &.{},
                                .for_empty = &.{},
                                .for_var = "",
                                .for_iter = .{ .parts = &.{} },
                                .for_iter_filters = &.{},
                                .var_path = .{ .parts = &.{} },
                                .set_name = ctx_mode,
                                .macro_params = include_bindings,
                                .macro_args = &.{},
                                .expr = null,
                                .ignore_missing = has_ignore_missing,
                            };
                            try nodes.append(allocator, node);
                        }
                    } else if (include_content.len >= 2 and include_content[0] == '[') {
                        // Fallback list: {% include ["a.html", "b.html"] %}
                        // Store comma-separated paths in block_name for render-time iteration
                        const end_bracket = std.mem.indexOf(u8, include_content, "]");
                        if (end_bracket) |eb| {
                            const list_content = include_content[1..eb];
                            // Build comma-separated path list (strip quotes from each entry)
                            var paths_buf: [2048]u8 = undefined;
                            var paths_pos: usize = 0;
                            var iter = std.mem.splitScalar(u8, list_content, ',');
                            var first = true;
                            while (iter.next()) |entry_raw| {
                                const entry = std.mem.trim(u8, entry_raw, " ");
                                if (entry.len >= 2 and (entry[0] == '\'' or entry[0] == '"')) {
                                    const path_str = entry[1 .. entry.len - 1];
                                    if (!first and paths_pos < paths_buf.len) {
                                        paths_buf[paths_pos] = ',';
                                        paths_pos += 1;
                                    }
                                    const copy_len = @min(path_str.len, paths_buf.len - paths_pos);
                                    @memcpy(paths_buf[paths_pos..][0..copy_len], path_str[0..copy_len]);
                                    paths_pos += copy_len;
                                    first = false;
                                }
                            }
                            const paths_str = try allocator.dupe(u8, paths_buf[0..paths_pos]);
                            try nodes.append(allocator, CompiledNode{
                                .type = .dynamic_include,
                                .text = "",
                                .block_name = paths_str, // comma-separated fallback paths
                                .children = &.{},
                                .filters = &.{},
                                .if_branches = &.{},
                                .for_body = &.{},
                                .for_empty = &.{},
                                .for_var = "",
                                .for_iter = .{ .parts = &.{} },
                                .for_iter_filters = &.{},
                                .var_path = .{ .parts = &.{} },
                                .set_name = ctx_mode,
                                .macro_params = include_bindings,
                                .macro_args = &.{},
                                .expr = null,
                                .ignore_missing = true, // fallback lists always ignore missing
                            });
                        }
                    } else if (include_content.len > 0) {
                        // Dynamic variable path — resolved at render time
                        const dyn_expr = try parseExpr(include_content);
                        try nodes.append(allocator, CompiledNode{
                            .type = .dynamic_include,
                            .text = "",
                            .block_name = "",
                            .children = &.{},
                            .filters = &.{},
                            .if_branches = &.{},
                            .for_body = &.{},
                            .for_empty = &.{},
                            .for_var = "",
                            .for_iter = .{ .parts = &.{} },
                            .for_iter_filters = &.{},
                            .var_path = .{ .parts = &.{} },
                            .set_name = ctx_mode,
                            .macro_params = include_bindings,
                            .macro_args = &.{},
                            .expr = dyn_expr,
                            .ignore_missing = has_ignore_missing,
                        });
                    }
                    start.* += 1;
                } else if (std.mem.startsWith(u8, tok.content, "autoescape ")) {
                    // {% autoescape true/false %}...{% endautoescape %}
                    const val = std.mem.trim(u8, tok.content[11..], " ");
                    // text="false" disables escaping, text="true" enables it
                    const ae_text = if (std.mem.eql(u8, val, "false") or std.mem.eql(u8, val, "off"))
                        "false"
                    else
                        "true";
                    start.* += 1;
                    const body = try parseNodes(tokens, start, &.{"endautoescape"});
                    if (start.* < tokens.len) start.* += 1; // skip endautoescape
                    try nodes.append(allocator, CompiledNode{
                        .type = .autoescape_block,
                        .text = ae_text,
                        .var_path = .{ .parts = &.{} },
                        .filters = &.{},
                        .if_branches = &.{},
                        .for_var = "",
                        .for_iter = .{ .parts = &.{} },
                        .for_iter_filters = &.{},
                        .for_body = &.{},
                        .for_empty = &.{},
                        .children = body,
                        .block_name = "",
                        .set_name = "",
                        .macro_params = &.{},
                        .macro_args = &.{},
                        .expr = null,
                    });
                } else if (std.mem.eql(u8, tok.content, "load") or std.mem.startsWith(u8, tok.content, "load ")) {
                    // {% load %} — Django compatibility, silently skip
                    start.* += 1;
                } else if (std.mem.eql(u8, tok.content, "break")) {
                    try nodes.append(allocator, CompiledNode{
                        .type = .break_stmt,
                        .text = "",
                        .var_path = .{ .parts = &.{} },
                        .filters = &.{},
                        .if_branches = &.{},
                        .for_var = "",
                        .for_iter = .{ .parts = &.{} },
                        .for_iter_filters = &.{},
                        .for_body = &.{},
                        .for_empty = &.{},
                        .children = &.{},
                        .block_name = "",
                        .set_name = "",
                        .macro_params = &.{},
                        .macro_args = &.{},
                        .expr = null,
                    });
                    start.* += 1;
                } else if (std.mem.eql(u8, tok.content, "continue")) {
                    try nodes.append(allocator, CompiledNode{
                        .type = .continue_stmt,
                        .text = "",
                        .var_path = .{ .parts = &.{} },
                        .filters = &.{},
                        .if_branches = &.{},
                        .for_var = "",
                        .for_iter = .{ .parts = &.{} },
                        .for_iter_filters = &.{},
                        .for_body = &.{},
                        .for_empty = &.{},
                        .children = &.{},
                        .block_name = "",
                        .set_name = "",
                        .macro_params = &.{},
                        .macro_args = &.{},
                        .expr = null,
                    });
                    start.* += 1;
                } else if (std.mem.startsWith(u8, tok.content, "do ")) {
                    // {% do expr %} — evaluate expression at compile time, discard result at render
                    const expr_str = std.mem.trim(u8, tok.content[3..], " ");
                    const do_expr = try parseExpr(expr_str);
                    try nodes.append(allocator, CompiledNode{
                        .type = .do_stmt,
                        .text = "",
                        .var_path = .{ .parts = &.{} },
                        .filters = &.{},
                        .if_branches = &.{},
                        .for_var = "",
                        .for_iter = .{ .parts = &.{} },
                        .for_iter_filters = &.{},
                        .for_body = &.{},
                        .for_empty = &.{},
                        .children = &.{},
                        .block_name = "",
                        .set_name = "",
                        .macro_params = &.{},
                        .macro_args = &.{},
                        .expr = do_expr,
                    });
                    start.* += 1;
                } else if (std.mem.eql(u8, tok.content, "debug")) {
                    // {% debug %} — dump context variables at render time
                    try nodes.append(allocator, CompiledNode{
                        .type = .debug_stmt,
                        .text = "",
                        .var_path = .{ .parts = &.{} },
                        .filters = &.{},
                        .if_branches = &.{},
                        .for_var = "",
                        .for_iter = .{ .parts = &.{} },
                        .for_iter_filters = &.{},
                        .for_body = &.{},
                        .for_empty = &.{},
                        .children = &.{},
                        .block_name = "",
                        .set_name = "",
                        .macro_params = &.{},
                        .macro_args = &.{},
                        .expr = null,
                    });
                    start.* += 1;
                } else if (std.mem.eql(u8, tok.content, "trans") or std.mem.startsWith(u8, tok.content, "trans ")) {
                    // {% trans %}...{% endtrans %} — i18n translation block
                    // Optional: {% trans name=expr %} for variable bindings
                    var trans_bindings_str: []const u8 = "";
                    if (tok.content.len > 5) {
                        trans_bindings_str = std.mem.trim(u8, tok.content[5..], " ");
                    }
                    start.* += 1;
                    const body = try parseNodes(tokens, start, &.{"endtrans"});
                    if (start.* < tokens.len) start.* += 1; // skip endtrans

                    // Parse optional variable bindings (name=expr, ...)
                    var trans_params: []MacroParam = &.{};
                    if (trans_bindings_str.len > 0) {
                        trans_params = try parseTransBindings(trans_bindings_str);
                    }

                    try nodes.append(allocator, CompiledNode{
                        .type = .trans_block,
                        .text = "",
                        .var_path = .{ .parts = &.{} },
                        .filters = &.{},
                        .if_branches = &.{},
                        .for_var = "",
                        .for_iter = .{ .parts = &.{} },
                        .for_iter_filters = &.{},
                        .for_body = &.{},
                        .for_empty = &.{},
                        .children = body,
                        .block_name = "",
                        .set_name = "",
                        .macro_params = trans_params,
                        .macro_args = &.{},
                        .expr = null,
                    });
                } else {
                    // Unknown tag — skip
                    start.* += 1;
                }
            },
            .comment => {
                start.* += 1;
            },
        }
    }

    return nodes.toOwnedSlice(allocator);
}

fn parseIfBlock(tokens: []const Token, pos: *usize) ParseError!CompiledNode {
    var branches: std.ArrayListUnmanaged(IfBranch) = .empty;

    // Parse initial {% if condition %}
    const first_cond = tokens[pos.*].content[3..]; // skip "if "
    pos.* += 1;

    const parsed = try parseCondition(std.mem.trim(u8, first_cond, " "));
    const if_body = try parseNodes(tokens, pos, &.{ "elif", "else", "endif" });

    try branches.append(allocator, .{
        .condition_expr = parsed.expr,
        .body = if_body,
    });

    // Parse elif/else branches
    while (pos.* < tokens.len) {
        const tok = tokens[pos.*];
        if (tok.type != .tag) break;

        if (std.mem.startsWith(u8, tok.content, "elif ")) {
            const elif_parsed = try parseCondition(std.mem.trim(u8, tok.content[5..], " "));
            pos.* += 1;
            const elif_body = try parseNodes(tokens, pos, &.{ "elif", "else", "endif" });
            try branches.append(allocator, .{
                .condition_expr = elif_parsed.expr,
                .body = elif_body,
            });
        } else if (std.mem.eql(u8, tok.content, "else")) {
            pos.* += 1;
            const else_body = try parseNodes(tokens, pos, &.{"endif"});
            try branches.append(allocator, .{
                .condition_expr = null,
                .body = else_body,
            });
        } else if (std.mem.eql(u8, tok.content, "endif")) {
            pos.* += 1;
            break;
        } else break;
    }

    return .{
        .type = .if_block,
        .text = "",
        .var_path = .{ .parts = &.{} },
        .filters = &.{},
        .if_branches = try branches.toOwnedSlice(allocator),
        .for_var = "",
        .for_iter = .{ .parts = &.{} },
        .for_iter_filters = &.{},
        .for_body = &.{},
        .for_empty = &.{},
        .children = &.{},
        .block_name = "",
        .set_name = "",
        .macro_params = &.{},
        .macro_args = &.{},
        .expr = null,
    };
}

// ── Expression system ────────────────────────────────────────────────────────
// Proper tokenizer + recursive descent parser with operator precedence.
// Handles all spacing variations (a+b, a + b, a  +  b), multi-char operators
// (**,//,==,!=,<=,>=), string literals, and parenthesized grouping.

/// Test types for `is` expressions — resolved at parse time, dispatched via switch at eval time.
const TestType = enum(i64) {
    unknown = 0,
    defined = 1,
    undefined = 2,
    none = 3,
    true_ = 4,
    false_ = 5,
    string = 6,
    number = 7,
    integer = 8,
    float_ = 9,
    boolean = 10,
    callable = 11,
    iterable = 12,
    mapping = 13,
    sequence = 14,
    odd = 15,
    even = 16,
    upper = 17,
    lower = 18,
    divisibleby = 19,
    sameas = 20,
    eq = 21,
    ne = 22,
    gt = 23,
    ge = 24,
    lt = 25,
    le = 26,

    fn fromName(name: []const u8) TestType {
        // Use first byte + length for fast pre-screening before full comparison
        if (name.len == 0) return .unknown;
        return switch (name[0]) {
            'b' => if (std.mem.eql(u8, name, "boolean")) .boolean else .unknown,
            'c' => if (std.mem.eql(u8, name, "callable")) .callable else .unknown,
            'd' => if (std.mem.eql(u8, name, "defined")) .defined else if (std.mem.eql(u8, name, "divisibleby")) .divisibleby else .unknown,
            'e' => if (std.mem.eql(u8, name, "even")) .even else if (std.mem.eql(u8, name, "eq")) .eq else if (std.mem.eql(u8, name, "equalto")) .eq else .unknown,
            'f' => if (std.mem.eql(u8, name, "false")) .false_ else if (std.mem.eql(u8, name, "float")) .float_ else .unknown,
            'g' => if (std.mem.eql(u8, name, "gt")) .gt else if (std.mem.eql(u8, name, "ge")) .ge else if (std.mem.eql(u8, name, "greaterthan")) .gt else .unknown,
            'i' => if (std.mem.eql(u8, name, "integer")) .integer else if (std.mem.eql(u8, name, "iterable")) .iterable else .unknown,
            'l' => if (std.mem.eql(u8, name, "lower")) .lower else if (std.mem.eql(u8, name, "lt")) .lt else if (std.mem.eql(u8, name, "le")) .le else if (std.mem.eql(u8, name, "lessthan")) .lt else .unknown,
            'm' => if (std.mem.eql(u8, name, "mapping")) .mapping else .unknown,
            'n' => if (std.mem.eql(u8, name, "none")) .none else if (std.mem.eql(u8, name, "number")) .number else if (std.mem.eql(u8, name, "ne")) .ne else .unknown,
            'o' => if (std.mem.eql(u8, name, "odd")) .odd else .unknown,
            's' => if (std.mem.eql(u8, name, "string")) .string else if (std.mem.eql(u8, name, "sequence")) .sequence else if (std.mem.eql(u8, name, "sameas")) .sameas else .unknown,
            't' => if (std.mem.eql(u8, name, "true")) .true_ else .unknown,
            'u' => if (std.mem.eql(u8, name, "undefined")) .undefined else if (std.mem.eql(u8, name, "upper")) .upper else .unknown,
            else => .unknown,
        };
    }
};

const ExprType = enum {
    literal_var, // variable path lookup
    literal_str, // string constant
    literal_int, // integer constant
    literal_float, // float constant
    binary_and, // left and right
    binary_or, // left or right
    unary_not, // not operand
    unary_neg, // -operand (unary minus)
    comparison, // left op right
    test_expr, // value is test_name
    in_expr, // value in container
    ternary, // true_val if condition else false_val
    math_add, // left + right
    math_sub, // left - right
    math_mul, // left * right
    math_div, // left / right
    math_floordiv, // left // right
    math_mod, // left % right
    math_pow, // left ** right
    string_concat, // left ~ right
    filter_expr, // left|filter(right) — left=input, str_val=name, right=arg
    func_call, // left(args) — left=callee, call_args=[*Expr]
    getattr_expr, // left.attr — left=object, str_val=attr name
    subscript_expr, // left[right] — left=object, right=index
    literal_list, // [a, b, c] — call_args = elements
    literal_dict, // {k: v, ...} — call_args = [k1, v1, k2, v2, ...]
    literal_tuple, // (a, b, c) — call_args = elements
};

const Expr = struct {
    type: ExprType,
    // Fields depend on type:
    var_path: VarPath, // literal_var
    str_val: []const u8, // literal_str, test name
    int_val: i64, // literal_int
    float_val: f64, // literal_float
    cmp_op: CompareOp, // comparison
    negate: bool, // unary_not, test_expr (is not)
    left: ?*Expr, // binary ops, comparison, in, math ops
    right: ?*Expr, // binary ops, comparison, in, math ops, ternary (condition)
    ternary_false: ?*Expr, // ternary false branch
    call_args: ?[]*Expr, // func_call: argument expressions

    fn deinit(self: *Expr) void {
        freeVarPath(&self.var_path);
        if (self.str_val.len > 0 and self.type != .literal_var) allocator.free(self.str_val);
        if (self.left) |l| {
            l.deinit();
            allocator.destroy(l);
        }
        if (self.right) |r| {
            r.deinit();
            allocator.destroy(r);
        }
        if (self.ternary_false) |tf| {
            tf.deinit();
            allocator.destroy(tf);
        }
        if (self.call_args) |args| {
            for (args) |a| {
                a.deinit();
                allocator.destroy(a);
            }
            allocator.free(args);
        }
    }
};

fn makeExpr() Expr {
    return .{
        .type = .literal_var,
        .var_path = .{ .parts = &.{} },
        .str_val = "",
        .int_val = 0,
        .float_val = 0.0,
        .cmp_op = .none,
        .negate = false,
        .left = null,
        .right = null,
        .ternary_false = null,
        .call_args = null,
    };
}

// ── Expression parser (token-based recursive descent) ────────────────────────
// All functions take *ExprParser (token stream), not raw strings.
// Left-associative operators use while loops. Right-associative use recursion.
//
// Precedence (lowest to highest):
//   ternary:    val if cond else val
//   or:         a or b
//   and:        a and b
//   not:        not a
//   comparison: ==, !=, <, >, <=, >=, is, is not, in, not in
//   add/sub:    a + b, a - b
//   concat:     a ~ b
//   mul/div:    a * b, a / b, a // b, a % b
//   power:      a ** b
//   unary:      -a, +a
//   postfix:    a.b, a[i], a(args), a|filter
//   primary:    name, int, float, string, True/False/None, (group)

// Legacy entry point for parseCondition (takes string, tokenizes internally)
fn parseExpr(s: []const u8) ParseError!*Expr {
    var tokens = try tokenizeExpr(s);
    defer tokens.deinit(allocator);
    var p = ExprParser{ .tokens = tokens.items, .pos = 0 };
    return exprParseTernary(&p);
}

// ── Token-based recursive descent functions ─────────────────────────────────

fn exprParseTernary(p: *ExprParser) ParseError!*Expr {
    const left = try exprParseOr(p);
    if (p.check(.kw_if)) {
        _ = p.advance();
        const cond = try exprParseOr(p);
        _ = try p.expect(.kw_else);
        const false_val = try exprParseOr(p);
        const expr = try allocator.create(Expr);
        expr.* = makeExpr();
        expr.type = .ternary;
        expr.left = left; // true value
        expr.right = cond; // condition
        expr.ternary_false = false_val;
        return expr;
    }
    return left;
}

fn exprParseOr(p: *ExprParser) ParseError!*Expr {
    var left = try exprParseAnd(p);
    while (p.check(.kw_or)) {
        _ = p.advance();
        const right = try exprParseAnd(p);
        const expr = try allocator.create(Expr);
        expr.* = makeExpr();
        expr.type = .binary_or;
        expr.left = left;
        expr.right = right;
        left = expr;
    }
    return left;
}

fn exprParseAnd(p: *ExprParser) ParseError!*Expr {
    var left = try exprParseNot(p);
    while (p.check(.kw_and)) {
        _ = p.advance();
        const right = try exprParseNot(p);
        const expr = try allocator.create(Expr);
        expr.* = makeExpr();
        expr.type = .binary_and;
        expr.left = left;
        expr.right = right;
        left = expr;
    }
    return left;
}

fn exprParseNot(p: *ExprParser) ParseError!*Expr {
    if (p.check(.kw_not)) {
        _ = p.advance();
        const inner = try exprParseCompare(p);
        const expr = try allocator.create(Expr);
        expr.* = makeExpr();
        expr.type = .unary_not;
        expr.left = inner;
        expr.negate = true;
        return expr;
    }
    return exprParseCompare(p);
}

fn exprParseCompare(p: *ExprParser) ParseError!*Expr {
    const left = try exprParsePipe(p);

    // Comparison operators
    const cmp_ops = [_]ExprTokenType{ .eq, .ne, .lt, .gt, .le, .ge };
    if (p.checkAny(&cmp_ops)) {
        const op = p.advance();
        const right = try exprParsePipe(p);
        const expr = try allocator.create(Expr);
        expr.* = makeExpr();
        expr.type = .comparison;
        expr.cmp_op = switch (op.type) {
            .eq => .eq,
            .ne => .ne,
            .lt => .lt,
            .gt => .gt,
            .le => .le,
            .ge => .ge,
            else => .none,
        };
        expr.left = left;
        expr.right = right;
        return expr;
    }

    // "is not" / "is" test
    if (p.check(.kw_is)) {
        _ = p.advance();
        var negate = false;
        if (p.check(.kw_not)) {
            _ = p.advance();
            negate = true;
        }
        const test_tok = p.advance(); // test name
        const test_type = TestType.fromName(test_tok.value);
        const expr = try allocator.create(Expr);
        expr.* = makeExpr();
        expr.type = .test_expr;
        expr.left = left;
        expr.int_val = @intFromEnum(test_type); // tokenized test type — O(1) switch dispatch
        expr.str_val = if (test_type == .unknown) try allocator.dupe(u8, test_tok.value) else "";
        expr.negate = negate;
        // Parse optional test argument: divisibleby(3), sameas(other)
        if (p.pos < p.tokens.len and p.tokens[p.pos].type == .lparen) {
            _ = p.advance(); // skip (
            expr.right = try exprParseOr(p); // parse argument expression
            if (p.pos < p.tokens.len and p.tokens[p.pos].type == .rparen) {
                _ = p.advance(); // skip )
            }
        }
        return expr;
    }

    // "not in" / "in"
    if (p.check(.kw_not) and p.pos + 1 < p.tokens.len and p.tokens[p.pos + 1].type == .kw_in) {
        _ = p.advance(); // not
        _ = p.advance(); // in
        const right = try exprParseAddSub(p);
        const expr = try allocator.create(Expr);
        expr.* = makeExpr();
        expr.type = .in_expr;
        expr.left = left;
        expr.right = right;
        expr.negate = true;
        return expr;
    }
    if (p.check(.kw_in)) {
        _ = p.advance();
        const right = try exprParseAddSub(p);
        const expr = try allocator.create(Expr);
        expr.* = makeExpr();
        expr.type = .in_expr;
        expr.left = left;
        expr.right = right;
        expr.negate = false;
        return expr;
    }

    return left;
}

/// Pipe operator for filters: `value|filter(arg)` — between comparison and add/sub precedence
fn exprParsePipe(p: *ExprParser) ParseError!*Expr {
    var left = try exprParseAddSub(p);

    while (p.check(.pipe)) {
        _ = p.advance();
        const filter_name_tok = p.advance();
        var filter_arg: ?*Expr = null;
        var filter_call_args: ?[]*Expr = null;
        if (p.pos < p.tokens.len and p.tokens[p.pos].type == .lparen) {
            _ = p.advance();
            if (!p.check(.rparen)) {
                var args: std.ArrayListUnmanaged(*Expr) = .empty;
                try args.append(allocator, try exprParseTernary(p));
                while (p.match(.comma)) {
                    if (p.check(.rparen)) break;
                    try args.append(allocator, try exprParseTernary(p));
                }
                if (args.items.len == 1) {
                    filter_arg = args.items[0];
                    args.deinit(allocator);
                } else {
                    filter_arg = args.items[0];
                    filter_call_args = try args.toOwnedSlice(allocator);
                }
            }
            _ = try p.expect(.rparen);
        }
        const expr = try allocator.create(Expr);
        expr.* = makeExpr();
        expr.type = .filter_expr;
        expr.left = left;
        expr.str_val = try allocator.dupe(u8, filter_name_tok.value);
        expr.right = filter_arg;
        expr.call_args = filter_call_args;
        left = expr;
    }
    return left;
}

fn exprParseAddSub(p: *ExprParser) ParseError!*Expr {
    var left = try exprParseConcat(p);
    while (p.check(.plus) or p.check(.minus)) {
        const op = p.advance();
        const right = try exprParseConcat(p);
        const expr = try allocator.create(Expr);
        expr.* = makeExpr();
        expr.type = if (op.type == .plus) .math_add else .math_sub;
        expr.left = left;
        expr.right = right;
        left = expr;
    }
    return left;
}

fn exprParseConcat(p: *ExprParser) ParseError!*Expr {
    var left = try exprParseMulDiv(p);
    while (p.check(.tilde)) {
        _ = p.advance();
        const right = try exprParseMulDiv(p);
        const expr = try allocator.create(Expr);
        expr.* = makeExpr();
        expr.type = .string_concat;
        expr.left = left;
        expr.right = right;
        left = expr;
    }
    return left;
}

fn exprParseMulDiv(p: *ExprParser) ParseError!*Expr {
    var left = try exprParsePow(p);
    const mul_ops = [_]ExprTokenType{ .star, .slash, .double_slash, .percent };
    while (p.checkAny(&mul_ops)) {
        const op = p.advance();
        const right = try exprParsePow(p);
        const expr = try allocator.create(Expr);
        expr.* = makeExpr();
        expr.type = switch (op.type) {
            .star => .math_mul,
            .slash => .math_div,
            .double_slash => .math_floordiv,
            .percent => .math_mod,
            else => .math_mul,
        };
        expr.left = left;
        expr.right = right;
        left = expr;
    }
    return left;
}

fn exprParsePow(p: *ExprParser) ParseError!*Expr {
    const left = try exprParseUnary(p);
    if (p.check(.double_star)) {
        _ = p.advance();
        const right = try exprParsePow(p); // right-associative
        const expr = try allocator.create(Expr);
        expr.* = makeExpr();
        expr.type = .math_pow;
        expr.left = left;
        expr.right = right;
        return expr;
    }
    return left;
}

fn exprParseUnary(p: *ExprParser) ParseError!*Expr {
    if (p.check(.minus)) {
        _ = p.advance();
        const inner = try exprParseUnary(p);
        const expr = try allocator.create(Expr);
        expr.* = makeExpr();
        expr.type = .unary_neg;
        expr.left = inner;
        return expr;
    }
    if (p.check(.plus)) {
        _ = p.advance();
        return exprParseUnary(p); // +x is a no-op
    }
    return exprParsePostfix(p);
}

/// Parse a single function call argument — handles both positional and keyword (name=value).
/// For keyword args like type='email', creates a literal_str with str_val = "type='email'"
/// so the macro call renderer can split on '=' to get the param name and value.
fn parseFuncCallArg(p: *ExprParser) ParseError!*Expr {
    // Check for keyword arg pattern: name = value
    // We peek ahead: if current is a name token and next is '=', it's a keyword arg
    const saved_pos = p.pos;
    if (p.check(.name)) {
        const name_tok = p.advance();
        if (p.check(.assign)) {
            _ = p.advance(); // consume '='
            const value_expr = try exprParseTernary(p);
            // Create a synthetic expression that encodes the keyword arg
            // For macro compatibility: serialize as "name=value" string
            const expr = try allocator.create(Expr);
            expr.* = makeExpr();
            expr.type = .literal_str;
            // Build "name='value'" or "name=value" string
            if (value_expr.type == .literal_str) {
                expr.str_val = try std.fmt.allocPrint(allocator, "{s}='{s}'", .{ name_tok.value, value_expr.str_val });
                value_expr.deinit();
                allocator.destroy(value_expr);
            } else if (value_expr.type == .literal_int) {
                expr.str_val = try std.fmt.allocPrint(allocator, "{s}={d}", .{ name_tok.value, value_expr.int_val });
                value_expr.deinit();
                allocator.destroy(value_expr);
            } else {
                expr.str_val = try std.fmt.allocPrint(allocator, "{s}=", .{name_tok.value});
                value_expr.deinit();
                allocator.destroy(value_expr);
            }
            return expr;
        }
        // Not a keyword arg — reset position
        p.pos = saved_pos;
    }
    // Positional arg
    return exprParseTernary(p);
}

/// Serialize an Expr back to string form for macro arg compatibility.
fn serializeExprToArgString(arg: *const Expr, buf: *std.ArrayListUnmanaged(u8)) !void {
    switch (arg.type) {
        .literal_str => {
            // Check if this is a keyword arg (contains '=')
            if (std.mem.indexOf(u8, arg.str_val, "=")) |_| {
                // Keyword arg — already formatted as "name='value'"
                try buf.appendSlice(allocator, arg.str_val);
            } else {
                try buf.append(allocator, '\'');
                try buf.appendSlice(allocator, arg.str_val);
                try buf.append(allocator, '\'');
            }
        },
        .literal_int => {
            var nbuf: [24]u8 = undefined;
            const ns = std.fmt.bufPrint(&nbuf, "{d}", .{arg.int_val}) catch "0";
            try buf.appendSlice(allocator, ns);
        },
        .literal_float => {
            var nbuf: [32]u8 = undefined;
            const ns = std.fmt.bufPrint(&nbuf, "{d}", .{arg.float_val}) catch "0";
            try buf.appendSlice(allocator, ns);
        },
        .literal_var => {
            for (arg.var_path.parts, 0..) |part, pi| {
                if (pi > 0) try buf.append(allocator, '.');
                try buf.appendSlice(allocator, part);
            }
        },
        else => {
            try buf.appendSlice(allocator, "''");
        },
    }
}

fn exprParsePostfix(p: *ExprParser) ParseError!*Expr {
    var left = try exprParsePrimary(p);
    while (true) {
        if (p.check(.dot)) {
            _ = p.advance();
            const attr_tok = p.advance();
            // Check for method call: .method() or .method(arg1, arg2)
            if (p.check(.lparen)) {
                _ = p.advance();
                // Parse arguments (may be empty) — supports keyword args
                var args = std.ArrayListUnmanaged(*Expr).empty;
                if (!p.check(.rparen)) {
                    try args.append(allocator, try parseFuncCallArg(p));
                    while (p.check(.comma)) {
                        _ = p.advance();
                        if (p.check(.rparen)) break; // trailing comma
                        try args.append(allocator, try parseFuncCallArg(p));
                    }
                }
                _ = try p.expect(.rparen);

                if (args.items.len == 0) {
                    // No-arg method: merge as "method()" into var_path
                    if (left.type == .literal_var) {
                        const method_name = try std.fmt.allocPrintSentinel(allocator, "{s}()", .{attr_tok.value}, 0);
                        const old = left.var_path.parts;
                        const new_parts = try allocator.alloc([:0]const u8, old.len + 1);
                        @memcpy(new_parts[0..old.len], old);
                        new_parts[old.len] = method_name;
                        if (old.len > 0) allocator.free(old);
                        left.var_path.parts = new_parts;
                    }
                    args.deinit(allocator);
                } else {
                    // Method with args: create func_call node
                    // First, build the callable: getattr(left, method_name)
                    const attr_expr = try allocator.create(Expr);
                    attr_expr.* = makeExpr();
                    attr_expr.type = .getattr_expr;
                    attr_expr.left = left;
                    attr_expr.str_val = try allocator.dupe(u8, attr_tok.value);

                    const call_expr = try allocator.create(Expr);
                    call_expr.* = makeExpr();
                    call_expr.type = .func_call;
                    call_expr.left = attr_expr;
                    call_expr.call_args = try allocator.dupe(*Expr, args.items);
                    args.deinit(allocator);
                    left = call_expr;
                }
            } else {
                // Simple attribute access — extend var_path
                if (left.type == .literal_var) {
                    const old = left.var_path.parts;
                    const new_parts = try allocator.alloc([:0]const u8, old.len + 1);
                    @memcpy(new_parts[0..old.len], old);
                    new_parts[old.len] = try allocator.dupeZ(u8, attr_tok.value);
                    if (old.len > 0) allocator.free(old);
                    left.var_path.parts = new_parts;
                } else {
                    // Non-var expr: create getattr
                    const expr = try allocator.create(Expr);
                    expr.* = makeExpr();
                    expr.type = .getattr_expr;
                    expr.left = left;
                    expr.str_val = try allocator.dupe(u8, attr_tok.value);
                    left = expr;
                }
            }
        } else if (p.check(.lbracket)) {
            _ = p.advance();
            // Subscript: build subscript string for var_path compatibility
            const index_expr = try exprParseTernary(p);
            _ = try p.expect(.rbracket);

            if (left.type == .literal_var) {
                // Build subscript notation: "items[0]" → append "[0]" to last part
                var sub_str: []const u8 = "";
                if (index_expr.type == .literal_int) {
                    sub_str = try std.fmt.allocPrint(allocator, "[{d}]", .{index_expr.int_val});
                } else if (index_expr.type == .unary_neg and index_expr.left != null and index_expr.left.?.type == .literal_int) {
                    // Negative index: -1 → [-1]
                    sub_str = try std.fmt.allocPrint(allocator, "[{d}]", .{-index_expr.left.?.int_val});
                } else if (index_expr.type == .literal_str) {
                    sub_str = try std.fmt.allocPrint(allocator, "['{s}']", .{index_expr.str_val});
                } else {
                    sub_str = try allocator.dupe(u8, "[0]");
                }
                // Append to last part of var_path (sentinel-terminated)
                if (left.var_path.parts.len > 0) {
                    const old_last = left.var_path.parts[left.var_path.parts.len - 1];
                    const new_last = try std.fmt.allocPrintSentinel(allocator, "{s}{s}", .{ old_last, sub_str }, 0);
                    allocator.free(old_last);
                    allocator.free(sub_str);
                    left.var_path.parts[left.var_path.parts.len - 1] = new_last;
                }
                index_expr.deinit();
                allocator.destroy(index_expr);
            } else {
                const expr = try allocator.create(Expr);
                expr.* = makeExpr();
                expr.type = .subscript_expr;
                expr.left = left;
                expr.right = index_expr;
                left = expr;
            }
        } else if (p.check(.lparen)) {
            _ = p.advance();
            // Function/macro call — supports positional and keyword args (name=value)
            var args: std.ArrayListUnmanaged(*Expr) = .empty;

            if (!p.check(.rparen)) {
                try args.append(allocator, try parseFuncCallArg(p));
                while (p.match(.comma)) {
                    if (p.check(.rparen)) break; // trailing comma
                    try args.append(allocator, try parseFuncCallArg(p));
                }
            }
            _ = try p.expect(.rparen);

            {
                // Always create func_call node for all calls.
                // Renderer dispatches: macros first (by name), then PyObject_CallObject.
                const expr = try allocator.create(Expr);
                expr.* = makeExpr();
                expr.type = .func_call;
                expr.left = left;
                expr.call_args = try args.toOwnedSlice(allocator);
                left = expr;
            }
        } else break;
        // Note: pipe operator `|` handled at correct precedence level in exprParsePipe
    }
    return left;
}

fn exprParsePrimary(p: *ExprParser) ParseError!*Expr {
    const tok = p.current();
    switch (tok.type) {
        .name => {
            _ = p.advance();
            const expr = try allocator.create(Expr);
            expr.* = makeExpr();
            expr.type = .literal_var;
            // Create single-part var path (sentinel-terminated — see VarPath comment)
            var parts = try allocator.alloc([:0]const u8, 1);
            parts[0] = try allocator.dupeZ(u8, tok.value);
            expr.var_path = .{ .parts = parts };
            return expr;
        },
        .integer_lit => {
            _ = p.advance();
            const expr = try allocator.create(Expr);
            expr.* = makeExpr();
            expr.type = .literal_int;
            expr.int_val = std.fmt.parseInt(i64, tok.value, 10) catch 0;
            return expr;
        },
        .float_lit => {
            _ = p.advance();
            const expr = try allocator.create(Expr);
            expr.* = makeExpr();
            expr.type = .literal_float;
            expr.float_val = std.fmt.parseFloat(f64, tok.value) catch 0.0;
            return expr;
        },
        .string_lit => {
            _ = p.advance();
            const expr = try allocator.create(Expr);
            expr.* = makeExpr();
            expr.type = .literal_str;
            // Strip quotes
            expr.str_val = try allocator.dupe(u8, tok.value[1 .. tok.value.len - 1]);
            return expr;
        },
        .kw_true => {
            _ = p.advance();
            const expr = try allocator.create(Expr);
            expr.* = makeExpr();
            expr.type = .literal_int;
            expr.int_val = 1;
            expr.str_val = try allocator.dupe(u8, "True");
            return expr;
        },
        .kw_false => {
            _ = p.advance();
            const expr = try allocator.create(Expr);
            expr.* = makeExpr();
            expr.type = .literal_int;
            expr.int_val = 0;
            expr.str_val = try allocator.dupe(u8, "False");
            return expr;
        },
        .kw_none => {
            _ = p.advance();
            const expr = try allocator.create(Expr);
            expr.* = makeExpr();
            expr.type = .literal_str;
            expr.str_val = try allocator.dupe(u8, "");
            return expr;
        },
        .lbracket => {
            // List literal: [a, b, c]
            _ = p.advance();
            var items: std.ArrayListUnmanaged(*Expr) = .empty;
            if (!p.check(.rbracket)) {
                try items.append(allocator, try exprParseTernary(p));
                while (p.match(.comma)) {
                    if (p.check(.rbracket)) break; // trailing comma
                    try items.append(allocator, try exprParseTernary(p));
                }
            }
            _ = try p.expect(.rbracket);
            const expr = try allocator.create(Expr);
            expr.* = makeExpr();
            expr.type = .literal_list;
            expr.call_args = try items.toOwnedSlice(allocator);
            return expr;
        },
        .lbrace => {
            // Dict literal: {k: v, k2: v2}
            _ = p.advance();
            var items: std.ArrayListUnmanaged(*Expr) = .empty;
            if (!p.check(.rbrace)) {
                // key: value pair
                try items.append(allocator, try exprParseTernary(p));
                _ = try p.expect(.colon);
                try items.append(allocator, try exprParseTernary(p));
                while (p.match(.comma)) {
                    if (p.check(.rbrace)) break;
                    try items.append(allocator, try exprParseTernary(p));
                    _ = try p.expect(.colon);
                    try items.append(allocator, try exprParseTernary(p));
                }
            }
            _ = try p.expect(.rbrace);
            const expr = try allocator.create(Expr);
            expr.* = makeExpr();
            expr.type = .literal_dict;
            expr.call_args = try items.toOwnedSlice(allocator);
            return expr;
        },
        .lparen => {
            _ = p.advance();
            const first = try exprParseTernary(p);
            // Check if this is a tuple: (a, b, c) vs grouped expr (a)
            if (p.check(.comma)) {
                // It's a tuple
                var items: std.ArrayListUnmanaged(*Expr) = .empty;
                try items.append(allocator, first);
                while (p.match(.comma)) {
                    if (p.check(.rparen)) break; // trailing comma
                    try items.append(allocator, try exprParseTernary(p));
                }
                _ = try p.expect(.rparen);
                const expr = try allocator.create(Expr);
                expr.* = makeExpr();
                expr.type = .literal_tuple;
                expr.call_args = try items.toOwnedSlice(allocator);
                return expr;
            }
            _ = try p.expect(.rparen);
            return first; // grouped expression
        },
        else => {
            // Fallback: try to treat as empty expression
            const expr = try allocator.create(Expr);
            expr.* = makeExpr();
            expr.type = .literal_str;
            expr.str_val = try allocator.dupe(u8, "");
            return expr;
        },
    }
}

// ── Expression evaluation ────────────────────────────────────────────────────

fn evalExpr(expr: *const Expr, context: *c.PyObject) ?*c.PyObject {
    switch (expr.type) {
        .literal_var => {
            return resolveVar(&expr.var_path, context);
        },
        .literal_str => {
            return py.newString(expr.str_val);
        },
        .literal_int => {
            return py.newInt(expr.int_val);
        },
        .binary_and => {
            const left = evalExpr(expr.left.?, context) orelse return py.pyFalse();
            if (c.PyObject_IsTrue(left) != 1) {
                return left; // Short-circuit: falsy left → return left
            }
            c.Py_DecRef(left);
            return evalExpr(expr.right.?, context) orelse py.pyFalse();
        },
        .binary_or => {
            const left = evalExpr(expr.left.?, context) orelse return py.pyFalse();
            if (c.PyObject_IsTrue(left) == 1) {
                return left; // Short-circuit: truthy left → return left
            }
            c.Py_DecRef(left);
            return evalExpr(expr.right.?, context) orelse py.pyFalse();
        },
        .unary_not => {
            const val = evalExpr(expr.left.?, context);
            if (val) |v| {
                defer c.Py_DecRef(v);
                return if (c.PyObject_IsTrue(v) != 1) py.pyTrue() else py.pyFalse();
            }
            return py.pyTrue(); // not undefined → True
        },
        .comparison => {
            const left = evalExpr(expr.left.?, context) orelse return py.pyFalse();
            defer c.Py_DecRef(left);
            const right = evalExpr(expr.right.?, context) orelse return py.pyFalse();
            defer c.Py_DecRef(right);

            const cmp_op: c_int = switch (expr.cmp_op) {
                .eq => c.Py_EQ,
                .ne => c.Py_NE,
                .lt => c.Py_LT,
                .gt => c.Py_GT,
                .le => c.Py_LE,
                .ge => c.Py_GE,
                .none => return if (c.PyObject_IsTrue(left) == 1) py.pyTrue() else py.pyFalse(),
            };
            return if (c.PyObject_RichCompareBool(left, right, cmp_op) == 1) py.pyTrue() else py.pyFalse();
        },
        .test_expr => {
            return evalTest(expr, context);
        },
        .in_expr => {
            const needle = evalExpr(expr.left.?, context) orelse return py.pyFalse();
            defer c.Py_DecRef(needle);
            const haystack = evalExpr(expr.right.?, context) orelse return py.pyFalse();
            defer c.Py_DecRef(haystack);
            const result = c.PySequence_Contains(haystack, needle);
            var truth = result == 1;
            if (expr.negate) truth = !truth;
            return if (truth) py.pyTrue() else py.pyFalse();
        },
        .literal_float => {
            return c.PyFloat_FromDouble(expr.float_val);
        },
        .ternary => {
            // true_val if condition else false_val
            const cond = evalExpr(expr.right.?, context);
            defer if (cond) |cv| c.Py_DecRef(cv);
            const truth = if (cond) |cv| c.PyObject_IsTrue(cv) == 1 else false;
            if (truth) {
                return evalExpr(expr.left.?, context);
            } else if (expr.ternary_false) |tf| {
                return evalExpr(tf, context);
            }
            return py.pyNone();
        },
        .unary_neg => {
            const val = evalExpr(expr.left.?, context) orelse return null;
            defer c.Py_DecRef(val);
            return c.PyNumber_Negative(val);
        },
        .math_add => {
            const left = evalExpr(expr.left.?, context) orelse return null;
            defer c.Py_DecRef(left);
            const right = evalExpr(expr.right.?, context) orelse return null;
            defer c.Py_DecRef(right);
            return c.PyNumber_Add(left, right);
        },
        .math_sub => {
            const left = evalExpr(expr.left.?, context) orelse return null;
            defer c.Py_DecRef(left);
            const right = evalExpr(expr.right.?, context) orelse return null;
            defer c.Py_DecRef(right);
            return c.PyNumber_Subtract(left, right);
        },
        .math_mul => {
            const left = evalExpr(expr.left.?, context) orelse return null;
            defer c.Py_DecRef(left);
            const right = evalExpr(expr.right.?, context) orelse return null;
            defer c.Py_DecRef(right);
            return c.PyNumber_Multiply(left, right);
        },
        .math_div => {
            const left = evalExpr(expr.left.?, context) orelse return null;
            defer c.Py_DecRef(left);
            const right = evalExpr(expr.right.?, context) orelse return null;
            defer c.Py_DecRef(right);
            return c.PyNumber_TrueDivide(left, right);
        },
        .math_floordiv => {
            const left = evalExpr(expr.left.?, context) orelse return null;
            defer c.Py_DecRef(left);
            const right = evalExpr(expr.right.?, context) orelse return null;
            defer c.Py_DecRef(right);
            return c.PyNumber_FloorDivide(left, right);
        },
        .math_mod => {
            const left = evalExpr(expr.left.?, context) orelse return null;
            defer c.Py_DecRef(left);
            const right = evalExpr(expr.right.?, context) orelse return null;
            defer c.Py_DecRef(right);
            return c.PyNumber_Remainder(left, right);
        },
        .math_pow => {
            const left = evalExpr(expr.left.?, context) orelse return null;
            defer c.Py_DecRef(left);
            const right = evalExpr(expr.right.?, context) orelse return null;
            defer c.Py_DecRef(right);
            // PyNumber_Power(base, exp, Py_None) — Py_None means no modulus
            const none = py.pyNone();
            defer c.Py_DecRef(none);
            return c.PyNumber_Power(left, right, none);
        },
        .string_concat => {
            const left = evalExpr(expr.left.?, context) orelse return py.newString("");
            defer c.Py_DecRef(left);
            const right = evalExpr(expr.right.?, context) orelse return py.newString("");
            defer c.Py_DecRef(right);
            // Convert both to strings if not already, then concatenate
            const left_str = if (c.PyUnicode_Check(left) != 0) blk: {
                c.Py_IncRef(left);
                break :blk left;
            } else c.PyObject_Str(left) orelse return null;
            defer c.Py_DecRef(left_str);
            const right_str = if (c.PyUnicode_Check(right) != 0) blk: {
                c.Py_IncRef(right);
                break :blk right;
            } else c.PyObject_Str(right) orelse return null;
            defer c.Py_DecRef(right_str);
            // PyUnicode_Concat
            return c.PyUnicode_Concat(left_str, right_str);
        },
        .filter_expr => {
            const input_val = evalExpr(expr.left.?, context) orelse return null;
            const filter_id = getNativeFilterId(expr.str_val);

            // Multi-arg filter: evaluate all args as Python objects, call method on value
            if (expr.call_args) |args| {
                // This path does getattr(value, filter_name) for any non-native
                // filter — so in sandbox mode it MUST honor the same block-list as
                // the other attribute paths, else `{{ x|__getattribute__('__class__') }}`
                // escapes the sandbox that exists to contain untrusted templates.
                if (isSandboxBlocked(expr.str_val)) {
                    c.Py_DecRef(input_val);
                    py.setError("SecurityError: filter '{s}' is blocked in sandbox mode", .{expr.str_val});
                    return null;
                }
                // Multi-arg filters (e.g., replace('old', 'new')) — use Python method dispatch
                const method_name_z = allocator.dupeZ(u8, expr.str_val) catch {
                    c.Py_DecRef(input_val);
                    return null;
                };
                defer allocator.free(method_name_z);
                // Get method from value object
                const method = c.PyObject_GetAttrString(input_val, method_name_z.ptr);
                if (method) |m| {
                    defer c.Py_DecRef(m);
                    const py_args = c.PyTuple_New(@intCast(args.len)) orelse {
                        c.Py_DecRef(input_val);
                        return null;
                    };
                    for (args, 0..) |arg_expr, i| {
                        const val = evalExpr(arg_expr, context) orelse py.pyNone();
                        _ = c.PyTuple_SetItem(py_args, @intCast(i), val);
                    }
                    const result = c.PyObject_CallObject(m, py_args);
                    c.Py_DecRef(py_args);
                    c.Py_DecRef(input_val);
                    if (result == null) c.PyErr_Clear();
                    return result;
                } else {
                    c.PyErr_Clear();
                    c.Py_DecRef(input_val);
                    return null;
                }
            }

            // Single-arg filter: extract arg string for native filter dispatch
            var arg_str: ?[]const u8 = null;
            if (expr.right) |arg_expr| {
                if (arg_expr.type == .literal_str) {
                    arg_str = arg_expr.str_val;
                } else if (arg_expr.type == .literal_int) {
                    var nbuf: [24]u8 = undefined;
                    arg_str = std.fmt.bufPrint(&nbuf, "{d}", .{arg_expr.int_val}) catch null;
                }
            }
            const result = applyNativeFilter(filter_id, input_val, arg_str);
            c.Py_DecRef(input_val);
            return result;
        },
        .func_call => {
            // Check for namespace macro call: m.input_field(args)
            // Pattern: func_call(getattr_expr(literal_var("m"), "input_field"), args)
            if (expr.left) |callee_expr| {
                if (callee_expr.type == .getattr_expr) {
                    if (callee_expr.left) |ns_expr| {
                        if (ns_expr.type == .literal_var and ns_expr.var_path.parts.len == 1) {
                            const ns_name = ns_expr.var_path.parts[0];
                            const macro_name = callee_expr.str_val;
                            // Build "m.input_field"
                            const prefixed = std.fmt.allocPrint(allocator, "{s}.{s}", .{ ns_name, macro_name }) catch return null;
                            defer allocator.free(prefixed);
                            if (current_template) |tmpl| {
                                if (tmpl.macros.get(prefixed)) |macro_idx| {
                                    if (macro_idx < tmpl.nodes.len) {
                                        const macro_node = &tmpl.nodes[macro_idx];
                                        // Create scoped context and bind args
                                        const macro_ctx = c.PyDict_Copy(context) orelse return null;
                                        defer c.Py_DecRef(macro_ctx);
                                        // Bind args to params — handle keyword args
                                        if (expr.call_args) |args| {
                                            // Separate positional and keyword args
                                            var positional = std.ArrayListUnmanaged(*Expr).empty;
                                            defer positional.deinit(allocator);
                                            var kwargs: std.StringHashMapUnmanaged([]const u8) = .{};
                                            defer kwargs.deinit(allocator);

                                            for (args) |arg| {
                                                if (arg.type == .literal_str) {
                                                    // Check for keyword arg: "type='password'"
                                                    if (std.mem.indexOf(u8, arg.str_val, "=")) |eq_pos| {
                                                        const kw_name = arg.str_val[0..eq_pos];
                                                        const kw_val = arg.str_val[eq_pos + 1 ..];
                                                        kwargs.put(allocator, kw_name, kw_val) catch {};
                                                        continue;
                                                    }
                                                }
                                                positional.append(allocator, arg) catch {};
                                            }

                                            for (macro_node.macro_params, 0..) |*param, pi| {
                                                const pk = allocator.dupeZ(u8, param.name) catch continue;
                                                defer allocator.free(pk);

                                                if (kwargs.get(param.name)) |kw_val| {
                                                    // Keyword arg
                                                    const val = resolveArgValue(kw_val, context) orelse py.pyNone();
                                                    _ = c.PyDict_SetItemString(macro_ctx, pk.ptr, val);
                                                    c.Py_DecRef(val);
                                                } else if (pi < positional.items.len) {
                                                    // Positional arg
                                                    const val = evalExpr(positional.items[pi], context) orelse py.pyNone();
                                                    _ = c.PyDict_SetItemString(macro_ctx, pk.ptr, val);
                                                    c.Py_DecRef(val);
                                                } else if (param.default_val) |def| {
                                                    const dv = resolveArgValue(def, context) orelse py.pyNone();
                                                    _ = c.PyDict_SetItemString(macro_ctx, pk.ptr, dv);
                                                    c.Py_DecRef(dv);
                                                }
                                            }
                                        }
                                        // Render macro into sub-buffer, return as string
                                        var sub_out = OutputBuffer.init();
                                        defer sub_out.deinit();
                                        _ = renderNodes(macro_node.children, macro_ctx, &sub_out, 0, null);
                                        return py.newString(sub_out.result());
                                    }
                                }
                            }
                        }
                    }
                }
            }

            // Priority 1: Simple macro call — func_call(literal_var("greet"), args)
            // Look up macro by simple name (macros shadow context variables)
            if (expr.left) |callee_expr| {
                if (callee_expr.type == .literal_var and callee_expr.var_path.parts.len == 1) {
                    const func_name = callee_expr.var_path.parts[0];
                    if (current_template) |tmpl| {
                        if (tmpl.macros.get(func_name)) |macro_idx| {
                            if (macro_idx < tmpl.nodes.len) {
                                const macro_node = &tmpl.nodes[macro_idx];
                                const macro_ctx = c.PyDict_Copy(context) orelse return null;
                                defer c.Py_DecRef(macro_ctx);
                                if (expr.call_args) |cargs| {
                                    // Separate positional and keyword args (same as namespace path)
                                    var positional = std.ArrayListUnmanaged(*Expr).empty;
                                    defer positional.deinit(allocator);
                                    var kwargs: std.StringHashMapUnmanaged([]const u8) = .{};
                                    defer kwargs.deinit(allocator);

                                    for (cargs) |arg| {
                                        if (arg.type == .literal_str) {
                                            if (std.mem.indexOf(u8, arg.str_val, "=")) |eq_pos| {
                                                const kw_name = arg.str_val[0..eq_pos];
                                                const kw_val = arg.str_val[eq_pos + 1 ..];
                                                kwargs.put(allocator, kw_name, kw_val) catch {};
                                                continue;
                                            }
                                        }
                                        positional.append(allocator, arg) catch {};
                                    }

                                    for (macro_node.macro_params, 0..) |*param, pi| {
                                        const pk = allocator.dupeZ(u8, param.name) catch continue;
                                        defer allocator.free(pk);
                                        if (kwargs.get(param.name)) |kw_val| {
                                            const val = resolveArgValue(kw_val, context) orelse py.pyNone();
                                            _ = c.PyDict_SetItemString(macro_ctx, pk.ptr, val);
                                            c.Py_DecRef(val);
                                        } else if (pi < positional.items.len) {
                                            const val = evalExpr(positional.items[pi], context) orelse py.pyNone();
                                            _ = c.PyDict_SetItemString(macro_ctx, pk.ptr, val);
                                            c.Py_DecRef(val);
                                        } else if (param.default_val) |def| {
                                            const dv = resolveArgValue(def, context) orelse py.pyNone();
                                            _ = c.PyDict_SetItemString(macro_ctx, pk.ptr, dv);
                                            c.Py_DecRef(dv);
                                        }
                                    }
                                }
                                var sub_out = OutputBuffer.init();
                                defer sub_out.deinit();
                                _ = renderNodes(macro_node.children, macro_ctx, &sub_out, 0, null);
                                return py.newString(sub_out.result());
                            }
                        }
                    }
                }
            }

            // Priority 2: Python callable — evaluate callee, call with args/kwargs
            const callee = evalExpr(expr.left.?, context) orelse return null;
            defer c.Py_DecRef(callee);
            // If callee is a dict with __call__ (e.g., recursive loop dict), extract the callable
            var actual_callee = callee;
            var owns_actual = false;
            if (c.PyDict_Check(callee) != 0) {
                if (c.PyDict_GetItemString(callee, "__call__")) |call_fn| {
                    c.Py_IncRef(call_fn); // borrowed → owned
                    actual_callee = call_fn;
                    owns_actual = true;
                }
            }
            defer if (owns_actual) c.Py_DecRef(actual_callee);

            if (c.PyCallable_Check(actual_callee) != 0) {
                if (expr.call_args) |args| {
                    // Separate positional args and keyword args
                    var positional = std.ArrayListUnmanaged(*c.PyObject).empty;
                    defer positional.deinit(allocator);
                    var kwargs_dict: ?*c.PyObject = null;

                    for (args) |arg| {
                        if (arg.type == .literal_str and arg.str_val.len > 0) {
                            // Check for "name=value" keyword arg pattern
                            if (std.mem.indexOf(u8, arg.str_val, "=")) |eq_pos| {
                                const kw_name = arg.str_val[0..eq_pos];
                                const kw_val_str = arg.str_val[eq_pos + 1 ..];
                                if (kwargs_dict == null) kwargs_dict = c.PyDict_New();
                                if (kwargs_dict) |kd| {
                                    // Parse value: strip quotes if present, else try int
                                    var kw_val: *c.PyObject = undefined;
                                    if (kw_val_str.len >= 2 and (kw_val_str[0] == '\'' or kw_val_str[0] == '"')) {
                                        kw_val = py.newString(kw_val_str[1 .. kw_val_str.len - 1]) orelse py.pyNone();
                                    } else {
                                        // Try integer
                                        const int_val = std.fmt.parseInt(i64, kw_val_str, 10) catch {
                                            // Try float
                                            const float_val = std.fmt.parseFloat(f64, kw_val_str) catch {
                                                // Try resolving as variable from context
                                                const var_key = allocator.dupeZ(u8, kw_val_str) catch continue;
                                                defer allocator.free(var_key);
                                                kw_val = c.PyMapping_GetItemString(context, var_key.ptr) orelse py.pyNone();
                                                const key_z = allocator.dupeZ(u8, kw_name) catch continue;
                                                defer allocator.free(key_z);
                                                _ = c.PyDict_SetItemString(kd, key_z.ptr, kw_val);
                                                c.Py_DecRef(kw_val);
                                                continue;
                                            };
                                            kw_val = c.PyFloat_FromDouble(float_val) orelse continue;
                                            const key_z2 = allocator.dupeZ(u8, kw_name) catch continue;
                                            defer allocator.free(key_z2);
                                            _ = c.PyDict_SetItemString(kd, key_z2.ptr, kw_val);
                                            c.Py_DecRef(kw_val);
                                            continue;
                                        };
                                        kw_val = py.newInt(int_val) orelse continue;
                                    }
                                    const key_z = allocator.dupeZ(u8, kw_name) catch continue;
                                    defer allocator.free(key_z);
                                    _ = c.PyDict_SetItemString(kd, key_z.ptr, kw_val);
                                    c.Py_DecRef(kw_val);
                                }
                                continue;
                            }
                        }
                        // Positional arg
                        const val = evalExpr(arg, context) orelse py.pyNone();
                        positional.append(allocator, val) catch {};
                    }

                    // Build args tuple from positional args only
                    const py_args = c.PyTuple_New(@intCast(positional.items.len)) orelse return null;
                    for (positional.items, 0..) |val, i| {
                        _ = c.PyTuple_SetItem(py_args, @intCast(i), val); // steals ref
                    }
                    // Call with or without kwargs
                    const result = if (kwargs_dict) |kd| blk: {
                        defer c.Py_DecRef(kd);
                        break :blk c.PyObject_Call(actual_callee, py_args, kd);
                    } else c.PyObject_CallObject(actual_callee, py_args);
                    c.Py_DecRef(py_args);
                    if (result == null) c.PyErr_Clear();
                    return result;
                }
                return c.PyObject_CallNoArgs(actual_callee);
            }
            return null;
        },
        .getattr_expr => {
            // Sandbox: block dangerous attribute access
            if (isSandboxBlocked(expr.str_val)) {
                py.setError("SecurityError: access to '{s}' is blocked in sandbox mode", .{expr.str_val});
                return null;
            }
            const obj = evalExpr(expr.left.?, context) orelse return null;
            defer c.Py_DecRef(obj);
            // Try dict key first (for loop dict, context dict, etc.)
            if (c.PyDict_Check(obj) != 0) {
                const attr_z = allocator.dupeZ(u8, expr.str_val) catch return null;
                defer allocator.free(attr_z);
                if (c.PyDict_GetItemString(obj, attr_z.ptr)) |item| {
                    c.Py_IncRef(item); // borrowed ref
                    return item;
                }
            }
            // Fall back to Python attribute lookup
            const attr_name = py.newString(expr.str_val) orelse return null;
            defer c.Py_DecRef(attr_name);
            const result = c.PyObject_GetAttr(obj, attr_name);
            if (result == null) c.PyErr_Clear();
            return result;
        },
        .subscript_expr => {
            const obj = evalExpr(expr.left.?, context) orelse return null;
            defer c.Py_DecRef(obj);
            const idx = evalExpr(expr.right.?, context) orelse return null;
            defer c.Py_DecRef(idx);
            const result = c.PyObject_GetItem(obj, idx);
            if (result == null) c.PyErr_Clear();
            return result;
        },
        .literal_list => {
            const args = expr.call_args orelse return c.PyList_New(0);
            const list = c.PyList_New(@intCast(args.len)) orelse return null;
            for (args, 0..) |item_expr, i| {
                const val = evalExpr(item_expr, context) orelse py.pyNone();
                _ = c.PyList_SetItem(list, @intCast(i), val); // steals ref
            }
            return list;
        },
        .literal_tuple => {
            const args = expr.call_args orelse return c.PyTuple_New(0);
            const tuple = c.PyTuple_New(@intCast(args.len)) orelse return null;
            for (args, 0..) |item_expr, i| {
                const val = evalExpr(item_expr, context) orelse py.pyNone();
                _ = c.PyTuple_SetItem(tuple, @intCast(i), val); // steals ref
            }
            return tuple;
        },
        .literal_dict => {
            // call_args stored as [k1, v1, k2, v2, ...]
            const args = expr.call_args orelse return c.PyDict_New();
            const dict = c.PyDict_New() orelse return null;
            var di: usize = 0;
            while (di + 1 < args.len) : (di += 2) {
                const key = evalExpr(args[di], context) orelse continue;
                const val = evalExpr(args[di + 1], context) orelse py.pyNone();
                _ = c.PyDict_SetItem(dict, key, val);
                c.Py_DecRef(key);
                c.Py_DecRef(val);
            }
            return dict;
        },
    }
}

fn evalTest(expr: *const Expr, context: *c.PyObject) ?*c.PyObject {
    const val = if (expr.left) |l| evalExpr(l, context) else null;
    defer if (val) |v| c.Py_DecRef(v);

    const test_type: TestType = @enumFromInt(expr.int_val);
    var result: bool = switch (test_type) {
        .defined => val != null,
        .undefined => val == null,
        .none => val != null and val.? == @as(*c.PyObject, @ptrCast(&c._Py_NoneStruct)),
        .true_ => val != null and c.PyObject_IsTrue(val.?) == 1,
        .false_ => val != null and c.PyObject_IsTrue(val.?) == 0,
        .string => val != null and c.PyUnicode_Check(val.?) != 0,
        .number, .integer => val != null and c.PyLong_Check(val.?) != 0,
        .float_ => val != null and c.PyFloat_Check(val.?) != 0,
        .boolean => val != null and c.PyBool_Check(val.?) != 0,
        .callable => val != null and c.PyCallable_Check(val.?) != 0,
        .sequence => val != null and c.PySequence_Check(val.?) != 0,
        .mapping => val != null and c.PyMapping_Check(val.?) != 0 and c.PyUnicode_Check(val.?) == 0,
        .iterable => blk: {
            if (val) |v| {
                const it = c.PyObject_GetIter(v);
                const ok = it != null;
                if (it) |i| c.Py_DecRef(i);
                c.PyErr_Clear();
                break :blk ok;
            }
            break :blk false;
        },
        .odd => if (val) |v| (c.PyLong_Check(v) != 0 and @mod(c.PyLong_AsLong(v), 2) != 0) else false,
        .even => if (val) |v| (c.PyLong_Check(v) != 0 and @mod(c.PyLong_AsLong(v), 2) == 0) else false,
        .upper => blk: {
            if (val) |v| {
                const m = c.PyObject_CallMethod(v, "isupper", null);
                const ok = m != null and c.PyObject_IsTrue(m) == 1;
                if (m) |mm| c.Py_DecRef(mm);
                break :blk ok;
            }
            break :blk false;
        },
        .lower => blk: {
            if (val) |v| {
                const m = c.PyObject_CallMethod(v, "islower", null);
                const ok = m != null and c.PyObject_IsTrue(m) == 1;
                if (m) |mm| c.Py_DecRef(mm);
                break :blk ok;
            }
            break :blk false;
        },
        .divisibleby => blk: {
            if (val) |v| {
                if (c.PyLong_Check(v) != 0) {
                    const num = c.PyLong_AsLong(v);
                    if (expr.right) |arg_expr| {
                        const arg = evalExpr(arg_expr, context);
                        if (arg) |a| {
                            defer c.Py_DecRef(a);
                            if (c.PyLong_Check(a) != 0) {
                                const divisor = c.PyLong_AsLong(a);
                                if (divisor != 0) break :blk @mod(num, divisor) == 0;
                            }
                        }
                    }
                }
            }
            break :blk false;
        },
        .sameas => blk: {
            if (expr.right) |arg_expr| {
                const arg = evalExpr(arg_expr, context);
                if (arg) |a| {
                    defer c.Py_DecRef(a);
                    if (val) |v| break :blk v == a;
                }
            }
            break :blk false;
        },
        .eq => evalTestComparison(val, expr.right, context, c.Py_EQ),
        .ne => evalTestComparison(val, expr.right, context, c.Py_NE),
        .gt => evalTestComparison(val, expr.right, context, c.Py_GT),
        .ge => evalTestComparison(val, expr.right, context, c.Py_GE),
        .lt => evalTestComparison(val, expr.right, context, c.Py_LT),
        .le => evalTestComparison(val, expr.right, context, c.Py_LE),
        .unknown => false,
    };

    if (expr.negate) result = !result;
    return if (result) py.pyTrue() else py.pyFalse();
}

/// Shared evaluator for comparison tests (eq, ne, gt, ge, lt, le).
/// Evaluates the test argument expression and applies PyObject_RichCompareBool.
fn evalTestComparison(val: ?*c.PyObject, arg_expr_opt: ?*Expr, context: *c.PyObject, op: c_int) bool {
    if (arg_expr_opt) |arg_expr| {
        const arg = evalExpr(arg_expr, context);
        if (arg) |a| {
            defer c.Py_DecRef(a);
            if (val) |v| return c.PyObject_RichCompareBool(v, a, op) == 1;
        }
    }
    return false;
}

// Legacy compat — IfBranch still uses condition VarPath + compare_op
// but we now parse conditions as expressions for and/or/not/is/in support

const ParsedCondition = struct {
    expr: ?*Expr,
};

fn parseCondition(cond_str: []const u8) ParseError!ParsedCondition {
    if (cond_str.len == 0) return .{ .expr = null };
    return .{ .expr = try parseExpr(cond_str) };
}

fn parseForBlock(tokens: []const Token, pos: *usize) ParseError!CompiledNode {
    // {% for item in items %} or {% for item in items|sort|reverse %}
    // Also: {% for x in [1,2,3] %}, {% for k,v in dict.items() %}
    // Also: {% for item in items recursive %}
    const content = tokens[pos.*].content;
    const in_pos = std.mem.indexOf(u8, content, " in ") orelse return error.BadForSyntax;
    const var_name = std.mem.trim(u8, content[4..in_pos], " "); // skip "for "
    var iter_expr_str = std.mem.trim(u8, content[in_pos + 4 ..], " ");

    // Check for "recursive" keyword suffix
    var is_recursive = false;
    if (std.mem.endsWith(u8, iter_expr_str, " recursive")) {
        is_recursive = true;
        iter_expr_str = std.mem.trim(u8, iter_expr_str[0 .. iter_expr_str.len - 10], " ");
    }

    // Parse iterable via expression parser (supports list literals, method calls, filters)
    const parsed = try parseExpressionFull(iter_expr_str);
    const is_simple = parsed.expr.type == .literal_var;
    pos.* += 1;

    const for_body = try parseNodes(tokens, pos, &.{ "empty", "else", "endfor" });

    var for_empty: []CompiledNode = &.{};
    if (pos.* < tokens.len and tokens[pos.*].type == .tag and
        (std.mem.eql(u8, tokens[pos.*].content, "empty") or std.mem.eql(u8, tokens[pos.*].content, "else")))
    {
        pos.* += 1;
        for_empty = try parseNodes(tokens, pos, &.{"endfor"});
    }
    if (pos.* < tokens.len and tokens[pos.*].type == .tag and std.mem.eql(u8, tokens[pos.*].content, "endfor")) {
        pos.* += 1;
    }

    return .{
        .type = .for_block,
        .text = if (is_recursive) "recursive" else "",
        .var_path = .{ .parts = &.{} },
        .filters = parsed.filters,
        .if_branches = &.{},
        .for_var = try allocator.dupe(u8, var_name),
        .for_iter = if (is_simple) parsed.expr.var_path else .{ .parts = &.{} },
        .for_iter_filters = &.{},
        .for_body = for_body,
        .for_empty = for_empty,
        .children = &.{},
        .block_name = "",
        .set_name = "",
        .macro_params = &.{},
        .macro_args = &.{},
        .expr = if (is_simple) null else parsed.expr,
    };
}

fn parseBlockDef(tokens: []const Token, pos: *usize) ParseError!CompiledNode {
    var name = std.mem.trim(u8, tokens[pos.*].content[6..], " "); // skip "block "
    // Check for "required" or "scoped" suffixes
    var is_required = false;
    var is_scoped = false;
    if (std.mem.endsWith(u8, name, " required")) {
        is_required = true;
        name = std.mem.trim(u8, name[0 .. name.len - 9], " ");
    }
    if (std.mem.endsWith(u8, name, " scoped")) {
        is_scoped = true;
        name = std.mem.trim(u8, name[0 .. name.len - 7], " ");
    }
    // Also check combined: "block name scoped required"
    if (std.mem.endsWith(u8, name, " required")) {
        is_required = true;
        name = std.mem.trim(u8, name[0 .. name.len - 9], " ");
    }
    if (std.mem.endsWith(u8, name, " scoped")) {
        is_scoped = true;
        name = std.mem.trim(u8, name[0 .. name.len - 7], " ");
    }
    pos.* += 1;
    const children = try parseNodes(tokens, pos, &.{"endblock"});
    if (pos.* < tokens.len) pos.* += 1; // skip endblock

    // text="required" marks this block as required (must be overridden by child)
    // for_var="scoped" marks this block as scoped (captures surrounding context)
    return .{
        .type = .block_def,
        .text = if (is_required) "required" else "",
        .var_path = .{ .parts = &.{} },
        .filters = &.{},
        .if_branches = &.{},
        .for_var = if (is_scoped) "scoped" else "",
        .for_iter = .{ .parts = &.{} },
        .for_iter_filters = &.{},
        .for_body = &.{},
        .for_empty = &.{},
        .children = children,
        .block_name = try allocator.dupe(u8, name),
        .set_name = "",
        .macro_params = &.{},
        .macro_args = &.{},
        .expr = null,
    };
}

/// Parse Django output tags: {% static 'path' %}, {% url 'name' arg %}, {% csrf_token %}
/// These produce output like {{ expr|filter }} — treated as variable nodes.
fn parseDjangoOutputTag(content: []const u8) ParseError!CompiledNode {
    if (std.mem.eql(u8, content, "csrf_token")) {
        // {% csrf_token %} → looks up 'csrf_token' from context
        // When Django's context processor sets csrf_token, this renders it
        const parsed = try parseExpressionFull("csrf_token");
        return .{
            .type = .variable,
            .text = "",
            .var_path = if (parsed.expr.type == .literal_var) parsed.expr.var_path else .{ .parts = &.{} },
            .filters = &.{},
            .if_branches = &.{},
            .for_var = "",
            .for_iter = .{ .parts = &.{} },
            .for_iter_filters = &.{},
            .for_body = &.{},
            .for_empty = &.{},
            .children = &.{},
            .block_name = "",
            .set_name = "",
            .macro_params = &.{},
            .macro_args = &.{},
            .expr = if (parsed.expr.type == .literal_var) null else parsed.expr,
        };
    }

    // {% static 'path' %} or {% url 'name' arg1 arg2 %}
    const space_pos = std.mem.indexOf(u8, content, " ") orelse return error.BadSetSyntax;
    const tag_name = content[0..space_pos];
    const arg_str = std.mem.trim(u8, content[space_pos + 1 ..], " ");

    // Build expression: arg_str|filter_name
    // e.g., {% static 'css/style.css' %} → 'css/style.css'|static
    var expr_buf: [512]u8 = undefined;
    const expr_str = std.fmt.bufPrint(&expr_buf, "{s}|{s}", .{ arg_str, tag_name }) catch arg_str;

    const parsed = try parseExpressionFull(expr_str);
    const is_simple = parsed.expr.type == .literal_var;
    return .{
        .type = .variable,
        .text = "",
        .var_path = if (is_simple) parsed.expr.var_path else .{ .parts = &.{} },
        .filters = parsed.filters,
        .if_branches = &.{},
        .for_var = "",
        .for_iter = .{ .parts = &.{} },
        .for_iter_filters = &.{},
        .for_body = &.{},
        .for_empty = &.{},
        .children = &.{},
        .block_name = "",
        .set_name = "",
        .macro_params = &.{},
        .macro_args = &.{},
        .expr = if (is_simple) null else parsed.expr,
    };
}

fn parseSetVar(content: []const u8) ParseError!CompiledNode {
    // {% set name = expr %} — supports full expressions (math, concat, etc.)
    // Find '=' but skip '==' comparison
    var eq_pos: ?usize = null;
    var i: usize = 4; // skip "set "
    while (i < content.len) : (i += 1) {
        if (content[i] == '=' and (i + 1 >= content.len or content[i + 1] != '=') and (i == 0 or content[i - 1] != '!')) {
            eq_pos = i;
            break;
        }
    }
    const eqp = eq_pos orelse return error.BadSetSyntax;
    const name = std.mem.trim(u8, content[4..eqp], " ");
    const expr_str = std.mem.trim(u8, content[eqp + 1 ..], " ");

    // Parse via expression parser (supports math, concat, etc.)
    const parsed = try parseExpressionFull(expr_str);
    const is_simple = parsed.expr.type == .literal_var;
    return .{
        .type = .set_var,
        .text = "",
        .var_path = if (is_simple) parsed.expr.var_path else .{ .parts = &.{} },
        .filters = parsed.filters,
        .if_branches = &.{},
        .for_var = "",
        .for_iter = .{ .parts = &.{} },
        .for_iter_filters = &.{},
        .for_body = &.{},
        .for_empty = &.{},
        .children = &.{},
        .block_name = "",
        .set_name = try allocator.dupe(u8, name),
        .macro_params = &.{},
        .macro_args = &.{},
        .expr = if (is_simple) null else parsed.expr,
    };
}

/// Parse {% macro name(param1, param2='default') %}body{% endmacro %}
fn parseMacroDef(tokens: []const Token, pos: *usize) ParseError!CompiledNode {
    const content = tokens[pos.*].content; // "macro name(param1, param2='default')"
    const macro_start = 6; // skip "macro "
    const paren_pos = std.mem.indexOf(u8, content[macro_start..], "(") orelse {
        // No parens — just name
        pos.* += 1;
        const body = try parseNodes(tokens, pos, &.{"endmacro"});
        if (pos.* < tokens.len) pos.* += 1;
        return makeNode(.macro_def, try allocator.dupe(u8, std.mem.trim(u8, content[macro_start..], " ")), body, &.{});
    };
    const name = std.mem.trim(u8, content[macro_start .. macro_start + paren_pos], " ");
    const close_paren = std.mem.indexOf(u8, content[macro_start + paren_pos ..], ")") orelse content.len - macro_start - paren_pos;
    const params_str = content[macro_start + paren_pos + 1 .. macro_start + paren_pos + close_paren];

    // Parse parameters
    var params: std.ArrayListUnmanaged(MacroParam) = .empty;
    if (params_str.len > 0) {
        var param_it = std.mem.splitScalar(u8, params_str, ',');
        while (param_it.next()) |raw_param| {
            const trimmed = std.mem.trim(u8, raw_param, " ");
            if (trimmed.len == 0) continue;
            // Check for default value: param='value' or param="value"
            if (std.mem.indexOf(u8, trimmed, "=")) |eq| {
                const pname = std.mem.trim(u8, trimmed[0..eq], " ");
                const default = std.mem.trim(u8, trimmed[eq + 1 ..], " ");
                // Keep quotes intact so resolveArgValue recognizes string literals
                try params.append(allocator, .{
                    .name = try allocator.dupe(u8, pname),
                    .default_val = try allocator.dupe(u8, default),
                });
            } else {
                try params.append(allocator, .{
                    .name = try allocator.dupe(u8, trimmed),
                    .default_val = null,
                });
            }
        }
    }

    pos.* += 1;
    const body = try parseNodes(tokens, pos, &.{"endmacro"});
    if (pos.* < tokens.len) pos.* += 1; // skip endmacro

    return .{
        .type = .macro_def,
        .text = "",
        .var_path = .{ .parts = &.{} },
        .filters = &.{},
        .if_branches = &.{},
        .for_var = "",
        .for_iter = .{ .parts = &.{} },
        .for_iter_filters = &.{},
        .for_body = &.{},
        .for_empty = &.{},
        .children = body,
        .block_name = try allocator.dupe(u8, name),
        .set_name = "",
        .macro_params = try params.toOwnedSlice(allocator),
        .macro_args = &.{},
        .expr = null,
    };
}

/// Parse {% call(item) macro_name(args) %}body{% endcall %}
fn parseCallBlock(tokens: []const Token, pos: *usize) ParseError!CompiledNode {
    // For now, parse as simplified: {% call macro_name(args) %}body{% endcall %}
    const content = tokens[pos.*].content;
    const call_start: usize = if (std.mem.startsWith(u8, content, "call ")) 5 else 4;
    const call_expr = std.mem.trim(u8, content[call_start..], " ");

    // Extract name and args from "macro_name(arg1, arg2)"
    var name: []const u8 = call_expr;
    var args_list: std.ArrayListUnmanaged([]const u8) = .empty;

    if (std.mem.indexOf(u8, call_expr, "(")) |paren| {
        name = call_expr[0..paren];
        const close = std.mem.lastIndexOf(u8, call_expr, ")") orelse call_expr.len;
        const args_str = call_expr[paren + 1 .. close];
        if (args_str.len > 0) {
            var it = std.mem.splitScalar(u8, args_str, ',');
            while (it.next()) |a| {
                const trimmed = std.mem.trim(u8, a, " ");
                if (trimmed.len > 0) try args_list.append(allocator, try allocator.dupe(u8, trimmed));
            }
        }
    }

    pos.* += 1;
    const body = try parseNodes(tokens, pos, &.{"endcall"});
    if (pos.* < tokens.len) pos.* += 1;

    return .{
        .type = .call_block,
        .text = "",
        .var_path = .{ .parts = &.{} },
        .filters = &.{},
        .if_branches = &.{},
        .for_var = "",
        .for_iter = .{ .parts = &.{} },
        .for_iter_filters = &.{},
        .for_body = &.{},
        .for_empty = &.{},
        .children = body,
        .block_name = try allocator.dupe(u8, name),
        .set_name = "",
        .macro_params = &.{},
        .macro_args = try args_list.toOwnedSlice(allocator),
        .expr = null,
    };
}

/// Parse "with key=expr, key2=expr2" bindings from an include tag.
/// Finds " with " after the template path (respecting quotes), extracts bindings,
/// and modifies include_raw to contain only the template path portion.
/// Returns empty slice if no bindings found.
fn parseIncludeBindings(include_raw: *[]const u8) ParseError![]MacroParam {
    const raw = include_raw.*;
    // Find " with " that's outside quotes, after the template path
    var in_quote: u8 = 0;
    var bracket_depth: i32 = 0;
    var with_pos: ?usize = null;
    var i: usize = 0;
    while (i < raw.len) : (i += 1) {
        const ch = raw[i];
        if (in_quote != 0) {
            if (ch == in_quote) in_quote = 0;
            continue;
        }
        if (ch == '\'' or ch == '"') {
            in_quote = ch;
            continue;
        }
        if (ch == '(' or ch == '[' or ch == '{') {
            bracket_depth += 1;
            continue;
        }
        if (ch == ')' or ch == ']' or ch == '}') {
            bracket_depth -= 1;
            continue;
        }
        // Look for " with " at top level
        if (ch == ' ' and bracket_depth == 0 and i + 6 <= raw.len) {
            if (std.mem.eql(u8, raw[i .. i + 6], " with ")) {
                // Check if what follows contains '=' (bindings) vs just "context"
                const after_with = std.mem.trim(u8, raw[i + 6 ..], " ");
                if (std.mem.eql(u8, after_with, "context")) {
                    // "with context" — not bindings, handled by caller
                    return &.{};
                }
                with_pos = i;
                break;
            }
        }
    }

    const wp = with_pos orelse return &.{};
    const bindings_str = std.mem.trim(u8, raw[wp + 6 ..], " ");
    include_raw.* = std.mem.trim(u8, raw[0..wp], " ");

    // Parse comma-separated name=value bindings (reuse same logic as parseWithBlock)
    var params: std.ArrayListUnmanaged(MacroParam) = .empty;
    if (bindings_str.len > 0) {
        var binding_starts: std.ArrayListUnmanaged(usize) = .empty;
        defer binding_starts.deinit(allocator);
        try binding_starts.append(allocator, 0);
        var depth: i32 = 0;
        var in_q: u8 = 0;
        for (bindings_str, 0..) |bch, idx| {
            if (in_q != 0) {
                if (bch == in_q) in_q = 0;
                continue;
            }
            if (bch == '\'' or bch == '"') {
                in_q = bch;
                continue;
            }
            if (bch == '(' or bch == '[' or bch == '{') {
                depth += 1;
                continue;
            }
            if (bch == ')' or bch == ']' or bch == '}') {
                depth -= 1;
                continue;
            }
            if (bch == ',' and depth == 0) {
                try binding_starts.append(allocator, idx + 1);
            }
        }

        for (binding_starts.items, 0..) |start_pos, bi| {
            const end_pos = if (bi + 1 < binding_starts.items.len)
                binding_starts.items[bi + 1] - 1
            else
                bindings_str.len;
            const binding = std.mem.trim(u8, bindings_str[start_pos..end_pos], " ");
            if (binding.len == 0) continue;

            // Find first '=' that's not '=='
            var eq: ?usize = null;
            var bd: i32 = 0;
            var bq: u8 = 0;
            for (binding, 0..) |bc, bi2| {
                if (bq != 0) {
                    if (bc == bq) bq = 0;
                    continue;
                }
                if (bc == '\'' or bc == '"') {
                    bq = bc;
                    continue;
                }
                if (bc == '(' or bc == '[' or bc == '{') {
                    bd += 1;
                    continue;
                }
                if (bc == ')' or bc == ']' or bc == '}') {
                    bd -= 1;
                    continue;
                }
                if (bc == '=' and bd == 0 and bi2 + 1 < binding.len and binding[bi2 + 1] != '=') {
                    if (bi2 > 0 and binding[bi2 - 1] == '!') continue;
                    eq = bi2;
                    break;
                }
            }
            const eqp = eq orelse continue;
            const name = std.mem.trim(u8, binding[0..eqp], " ");
            const val = std.mem.trim(u8, binding[eqp + 1 ..], " ");
            try params.append(allocator, .{
                .name = try allocator.dupe(u8, name),
                .default_val = try allocator.dupe(u8, val),
            });
        }
    }
    return try params.toOwnedSlice(allocator);
}

/// Parse comma-separated name=value bindings for {% trans name=expr, ... %}.
/// Same logic as the binding portion of parseWithBlock.
fn parseTransBindings(bindings_str: []const u8) ParseError![]MacroParam {
    var params: std.ArrayListUnmanaged(MacroParam) = .empty;
    if (bindings_str.len == 0) return &.{};

    var binding_starts: std.ArrayListUnmanaged(usize) = .empty;
    defer binding_starts.deinit(allocator);
    try binding_starts.append(allocator, 0);
    var depth: i32 = 0;
    var in_q: u8 = 0;
    for (bindings_str, 0..) |ch, idx| {
        if (in_q != 0) {
            if (ch == in_q) in_q = 0;
            continue;
        }
        if (ch == '\'' or ch == '"') {
            in_q = ch;
            continue;
        }
        if (ch == '(' or ch == '[' or ch == '{') {
            depth += 1;
            continue;
        }
        if (ch == ')' or ch == ']' or ch == '}') {
            depth -= 1;
            continue;
        }
        if (ch == ',' and depth == 0) {
            try binding_starts.append(allocator, idx + 1);
        }
    }

    for (binding_starts.items, 0..) |start_pos, bi| {
        const end_pos = if (bi + 1 < binding_starts.items.len)
            binding_starts.items[bi + 1] - 1
        else
            bindings_str.len;
        const binding = std.mem.trim(u8, bindings_str[start_pos..end_pos], " ");
        if (binding.len == 0) continue;

        var eq: ?usize = null;
        var bd: i32 = 0;
        var bq: u8 = 0;
        for (binding, 0..) |bc, bi2| {
            if (bq != 0) {
                if (bc == bq) bq = 0;
                continue;
            }
            if (bc == '\'' or bc == '"') {
                bq = bc;
                continue;
            }
            if (bc == '(' or bc == '[' or bc == '{') {
                bd += 1;
                continue;
            }
            if (bc == ')' or bc == ']' or bc == '}') {
                bd -= 1;
                continue;
            }
            if (bc == '=' and bd == 0 and bi2 + 1 < binding.len and binding[bi2 + 1] != '=') {
                if (bi2 > 0 and binding[bi2 - 1] == '!') continue;
                eq = bi2;
                break;
            }
        }
        const eqp = eq orelse continue;
        const name = std.mem.trim(u8, binding[0..eqp], " ");
        const val = std.mem.trim(u8, binding[eqp + 1 ..], " ");
        try params.append(allocator, .{
            .name = try allocator.dupe(u8, name),
            .default_val = try allocator.dupe(u8, val),
        });
    }
    return try params.toOwnedSlice(allocator);
}

/// Parse {% with x=expr, y=expr %}body{% endwith %}
/// Stores variable bindings as macro_params (name=default_val pairs)
/// and body as children.
fn parseWithBlock(tokens: []const Token, pos: *usize) ParseError!CompiledNode {
    const content = tokens[pos.*].content;
    // Extract bindings: "with x=1, y='hello'"
    const bindings_str = if (content.len > 5) content[5..] else ""; // skip "with "
    pos.* += 1;

    // Parse comma-separated name=value bindings (respecting brackets/parens/quotes)
    var params: std.ArrayListUnmanaged(MacroParam) = .empty;
    if (bindings_str.len > 0) {
        // Split on top-level commas (not inside brackets/parens/quotes)
        var binding_starts: std.ArrayListUnmanaged(usize) = .empty;
        defer binding_starts.deinit(allocator);
        try binding_starts.append(allocator, 0);
        var depth: i32 = 0;
        var in_quotes: u8 = 0;
        for (bindings_str, 0..) |ch, idx| {
            if (in_quotes != 0) {
                if (ch == in_quotes) in_quotes = 0;
                continue;
            }
            if (ch == '\'' or ch == '"') {
                in_quotes = ch;
                continue;
            }
            if (ch == '(' or ch == '[' or ch == '{') {
                depth += 1;
                continue;
            }
            if (ch == ')' or ch == ']' or ch == '}') {
                depth -= 1;
                continue;
            }
            if (ch == ',' and depth == 0) {
                try binding_starts.append(allocator, idx + 1);
            }
        }

        for (binding_starts.items, 0..) |start_pos, bi| {
            const end_pos = if (bi + 1 < binding_starts.items.len)
                binding_starts.items[bi + 1] - 1 // -1 to skip the comma
            else
                bindings_str.len;
            const binding = std.mem.trim(u8, bindings_str[start_pos..end_pos], " ");
            if (binding.len == 0) continue;

            // Find first '=' that's not '=='
            var eq: ?usize = null;
            var bd: i32 = 0;
            var bq: u8 = 0;
            for (binding, 0..) |bch, bi2| {
                if (bq != 0) {
                    if (bch == bq) bq = 0;
                    continue;
                }
                if (bch == '\'' or bch == '"') {
                    bq = bch;
                    continue;
                }
                if (bch == '(' or bch == '[' or bch == '{') {
                    bd += 1;
                    continue;
                }
                if (bch == ')' or bch == ']' or bch == '}') {
                    bd -= 1;
                    continue;
                }
                if (bch == '=' and bd == 0 and bi2 + 1 < binding.len and binding[bi2 + 1] != '=') {
                    if (bi2 > 0 and binding[bi2 - 1] == '!') continue;
                    eq = bi2;
                    break;
                }
            }
            const eqp = eq orelse continue;
            const name = std.mem.trim(u8, binding[0..eqp], " ");
            const val = std.mem.trim(u8, binding[eqp + 1 ..], " ");
            try params.append(allocator, .{
                .name = try allocator.dupe(u8, name),
                .default_val = try allocator.dupe(u8, val),
            });
        }
    }

    const body = try parseNodes(tokens, pos, &.{"endwith"});
    if (pos.* < tokens.len) pos.* += 1; // skip endwith

    return .{
        .type = .with_block,
        .text = "",
        .var_path = .{ .parts = &.{} },
        .filters = &.{},
        .if_branches = &.{},
        .for_var = "",
        .for_iter = .{ .parts = &.{} },
        .for_iter_filters = &.{},
        .for_body = &.{},
        .for_empty = &.{},
        .children = body,
        .block_name = "",
        .set_name = "",
        .macro_params = try params.toOwnedSlice(allocator),
        .macro_args = &.{},
        .expr = null,
    };
}

fn makeNode(t: NodeType, name: []const u8, children: []CompiledNode, params: []MacroParam) CompiledNode {
    return .{
        .type = t,
        .text = "",
        .var_path = .{ .parts = &.{} },
        .filters = &.{},
        .if_branches = &.{},
        .for_var = "",
        .for_iter = .{ .parts = &.{} },
        .for_iter_filters = &.{},
        .for_body = &.{},
        .for_empty = &.{},
        .children = children,
        .block_name = name,
        .set_name = "",
        .macro_params = params,
        .macro_args = &.{},
        .expr = null,
    };
}

// ── Compilation entry point ──────────────────────────────────────────────────

/// Recursively walk node tree and index all block_def nodes by name → pointer.
/// This finds blocks nested inside for-loops, if-blocks, with-blocks, etc.
fn indexBlocksRecursive(nodes: []CompiledNode, blocks: *std.StringHashMapUnmanaged(*CompiledNode)) !void {
    for (nodes) |*node| {
        if (node.type == .block_def and node.block_name.len > 0) {
            try blocks.put(allocator, node.block_name, node);
        }
        // Recurse into child structures
        if (node.children.len > 0) try indexBlocksRecursive(node.children, blocks);
        if (node.for_body.len > 0) try indexBlocksRecursive(node.for_body, blocks);
        if (node.for_empty.len > 0) try indexBlocksRecursive(node.for_empty, blocks);
        for (node.if_branches) |*branch| {
            if (branch.body.len > 0) try indexBlocksRecursive(branch.body, blocks);
        }
    }
}

pub fn compile(source: []const u8, path: []const u8) (ParseError || error{TokenizeError})!*CompiledTemplate {
    // Save thread-local directive state (for recursive compile calls via extends/import)
    const saved_extends = extends_parent_path;
    const saved_dynamic_extends = extends_dynamic_expr;
    const saved_imports = import_directives;
    const saved_from_imports = from_import_directives;

    // Reset for this compilation
    extends_parent_path = null;
    extends_dynamic_expr = null;
    import_directives = .empty;
    from_import_directives = .empty;

    // Restore on exit (needed if we return early due to extends)
    defer {
        extends_parent_path = saved_extends;
        extends_dynamic_expr = saved_dynamic_extends;
        import_directives = saved_imports;
        from_import_directives = saved_from_imports;
    }

    var tokens = try tokenize(source);
    defer {
        for (tokens.items) |*t| allocator.free(t.content);
        tokens.deinit(allocator);
    }

    var pos: usize = 0;
    var nodes = try parseNodes(tokens.items, &pos, &.{});

    // ── Handle {% extends "parent.html" %} ──────────────────────────
    // If child extends a parent, load parent via Python loader, compile it,
    // and merge: use parent's node tree with child's blocks overriding parent's.
    if (extends_parent_path) |parent_path| {
        defer {
            allocator.free(parent_path);
            extends_parent_path = null;
        }

        const loader = template_loader orelse {
            py.setError("Template uses {s} extends \"{s}{s} but no template loader is configured", .{ "%", parent_path, "%" });
            return error.BadSyntax;
        };
        {
            // Call Python loader: loader(parent_path) → source string
            const py_path = py.newString(parent_path) orelse return error.OutOfMemory;
            defer c.Py_DecRef(py_path);
            const py_args = c.PyTuple_Pack(1, py_path) orelse return error.OutOfMemory;
            defer c.Py_DecRef(py_args);
            const py_result = c.PyObject_CallObject(loader, py_args);

            if (py_result) |parent_source_obj| {
                defer c.Py_DecRef(parent_source_obj);

                if (c.PyUnicode_Check(parent_source_obj) != 0) {
                    if (c.PyUnicode_AsUTF8(parent_source_obj)) |parent_src| {
                        const parent_source = std.mem.span(parent_src);

                        // Compile parent template (recursive — handles nested extends)
                        const parent_tmpl = try compile(parent_source, parent_path);

                        // Collect child's block definitions
                        var child_blocks: std.StringHashMapUnmanaged(usize) = .{};
                        for (nodes, 0..) |node, i| {
                            if (node.type == .block_def and node.block_name.len > 0) {
                                try child_blocks.put(allocator, node.block_name, i);
                            }
                        }

                        // Override parent blocks with child blocks, preserving parent content for super()
                        var iter = child_blocks.iterator();
                        while (iter.next()) |entry| {
                            const block_name = entry.key_ptr.*;
                            const child_idx = entry.value_ptr.*;

                            if (parent_tmpl.blocks.get(block_name)) |parent_node| {
                                const child_node = &nodes[child_idx];

                                // Clone parent's current children into super_children
                                parent_node.super_children = deepCloneNodes(parent_node.children);

                                // Transfer child's children to parent block
                                parent_node.children = child_node.children;
                            }
                        }

                        // Validate required blocks — iterate parent block registry
                        var block_iter = parent_tmpl.blocks.iterator();
                        while (block_iter.next()) |bentry| {
                            const pnode = bentry.value_ptr.*;
                            if (std.mem.eql(u8, pnode.text, "required")) {
                                if (!child_blocks.contains(bentry.key_ptr.*)) {
                                    py.setError("Required block '{s}' not overridden in child template", .{bentry.key_ptr.*});
                                    return error.BadSyntax;
                                }
                            }
                        }

                        // Also copy child macros into parent
                        for (nodes) |node| {
                            if (node.type == .macro_def and node.block_name.len > 0) {
                                try parent_tmpl.macros.put(allocator, node.block_name, parent_tmpl.nodes.len); // TODO: proper index
                            }
                        }

                        // Free child nodes that were NOT transferred to parent.
                        // Block_def children were moved to parent — null them to prevent double-free.
                        for (nodes) |*cnode| {
                            if (cnode.type == .block_def) {
                                // Children transferred to parent — don't free them here
                                cnode.children = &.{};
                            }
                        }
                        freeNodes(nodes);
                        allocator.free(nodes);

                        // The parent template IS the result (with overridden blocks)
                        return parent_tmpl;
                    }
                }
            } else {
                c.PyErr_Clear();
            }
        }
    }

    // ── Handle dynamic {% extends variable %} ──────────────────────────
    // Expression stored for render-time resolution. The child's nodes (including
    // block definitions) are preserved on the CompiledTemplate for block merging
    // at render time when the parent path is known.
    const dyn_ext_expr = extends_dynamic_expr;
    extends_dynamic_expr = null;

    // ── Handle {% import %} and {% from %} ──────────────────────────
    // Track extra macros added from imports (stored after initial build)
    var extra_macros: std.StringHashMapUnmanaged(usize) = .{};

    if (template_loader) |loader| {
        for (import_directives.items) |directive| {
            const py_path = py.newString(directive.path) orelse continue;
            defer c.Py_DecRef(py_path);
            const py_args = c.PyTuple_Pack(1, py_path) orelse continue;
            defer c.Py_DecRef(py_args);
            const py_result = c.PyObject_CallObject(loader, py_args);
            if (py_result) |src_obj| {
                defer c.Py_DecRef(src_obj);
                if (c.PyUnicode_Check(src_obj) != 0) {
                    if (c.PyUnicode_AsUTF8(src_obj)) |src| {
                        const imported = compile(std.mem.span(src), directive.path) catch continue;
                        defer {
                            // Fully free imported template — we deep-clone what we need
                            imported.deinit();
                            allocator.destroy(imported);
                        }
                        // Deep-clone imported macros with alias prefix: "m.macro_name"
                        var macro_iter = imported.macros.iterator();
                        while (macro_iter.next()) |entry| {
                            const prefixed = std.fmt.allocPrint(allocator, "{s}.{s}", .{ directive.alias, entry.key_ptr.* }) catch continue;
                            const macro_idx = entry.value_ptr.*;
                            if (macro_idx < imported.nodes.len) {
                                // Deep-clone the macro node so we fully own it
                                const cloned_slice = deepCloneNodes(imported.nodes[macro_idx .. macro_idx + 1]);
                                if (cloned_slice.len == 0) continue;
                                defer allocator.free(cloned_slice); // free the wrapper slice, node is appended below
                                // Set block_name to prefixed name so bytecode deserialization
                                // can rebuild the macro index correctly
                                cloned_slice[0].block_name = prefixed;
                                var node_list = std.ArrayListUnmanaged(CompiledNode).fromOwnedSlice(nodes);
                                const new_idx = node_list.items.len;
                                node_list.append(allocator, cloned_slice[0]) catch continue;
                                nodes = node_list.toOwnedSlice(allocator) catch nodes;
                                // Register with prefixed name
                                extra_macros.put(allocator, prefixed, new_idx) catch {};
                            }
                        }
                    }
                }
            } else {
                c.PyErr_Clear();
            }
        }

        for (from_import_directives.items) |directive| {
            const py_path = py.newString(directive.path) orelse continue;
            defer c.Py_DecRef(py_path);
            const py_args = c.PyTuple_Pack(1, py_path) orelse continue;
            defer c.Py_DecRef(py_args);
            const py_result = c.PyObject_CallObject(loader, py_args);
            if (py_result) |src_obj| {
                defer c.Py_DecRef(src_obj);
                if (c.PyUnicode_Check(src_obj) != 0) {
                    if (c.PyUnicode_AsUTF8(src_obj)) |src| {
                        const imported = compile(std.mem.span(src), directive.path) catch continue;
                        defer {
                            imported.deinit();
                            allocator.destroy(imported);
                        }
                        // Deep-clone specific macro by name into our template
                        if (imported.macros.get(directive.name)) |macro_idx| {
                            if (macro_idx < imported.nodes.len) {
                                const cloned_slice = deepCloneNodes(imported.nodes[macro_idx .. macro_idx + 1]);
                                if (cloned_slice.len > 0) {
                                    defer allocator.free(cloned_slice);
                                    var node_list = std.ArrayListUnmanaged(CompiledNode).fromOwnedSlice(nodes);
                                    node_list.append(allocator, cloned_slice[0]) catch {};
                                    nodes = node_list.toOwnedSlice(allocator) catch nodes;
                                }
                            }
                        }
                    }
                }
            } else {
                c.PyErr_Clear();
            }
        }
    }

    // ── Handle {% include "partial.html" %} ──────────────────────────
    // Walk nodes and replace .include nodes with compiled content from the included file.
    if (template_loader) |loader| {
        resolveIncludes(nodes, loader);
    }

    // Note: directive storage cleanup is handled by defer at function start

    const duped_path = try allocator.dupe(u8, path);
    const tmpl = try allocator.create(CompiledTemplate);
    tmpl.* = CompiledTemplate{
        .nodes = nodes,
        .blocks = .{},
        .macros = .{},
        .source_path = duped_path,
        .py_filters = .{},
        .dynamic_extends_expr = dyn_ext_expr,
        .dynamic_extends_child_nodes = if (dyn_ext_expr != null) deepCloneNodes(nodes) else null,
    };

    // Build block + macro indices (recursive — finds blocks inside for-loops, if-blocks, etc.)
    try indexBlocksRecursive(tmpl.nodes, &tmpl.blocks);
    for (nodes, 0..) |node, i| {
        if (node.type == .macro_def and node.block_name.len > 0) {
            try tmpl.macros.put(allocator, node.block_name, i);
        }
    }

    // Add imported macros (from {% import "x" as m %} and {% from "x" import y %})
    var extra_iter = extra_macros.iterator();
    while (extra_iter.next()) |entry| {
        try tmpl.macros.put(allocator, entry.key_ptr.*, entry.value_ptr.*);
    }
    extra_macros.deinit(allocator);

    return tmpl;
}

// ── Include resolution ───────────────────────────────────────────────────────

/// Walk nodes and resolve {% include "path" %} by loading and compiling the included template.
/// The include node's children are set to the compiled nodes from the included file.
fn resolveIncludes(nodes: []CompiledNode, loader: *c.PyObject) void {
    for (nodes) |*node| {
        switch (node.type) {
            .include => {
                // Load included template via Python callback
                if (node.block_name.len > 0) {
                    const py_path = py.newString(node.block_name) orelse continue;
                    defer c.Py_DecRef(py_path);
                    const py_args = c.PyTuple_Pack(1, py_path) orelse continue;
                    defer c.Py_DecRef(py_args);
                    const py_result = c.PyObject_CallObject(loader, py_args);
                    if (py_result) |src_obj| {
                        defer c.Py_DecRef(src_obj);
                        if (c.PyUnicode_Check(src_obj) != 0) {
                            if (c.PyUnicode_AsUTF8(src_obj)) |src| {
                                const included_source = std.mem.span(src);
                                const included_tmpl = compile(included_source, node.block_name) catch continue;
                                // Take ownership of the compiled nodes
                                node.children = included_tmpl.nodes;
                                // Free the template shell WITHOUT freeing the nodes we just took
                                included_tmpl.blocks.deinit(allocator);
                                included_tmpl.macros.deinit(allocator);
                                included_tmpl.py_filters.deinit(allocator);
                                if (included_tmpl.loader) |l| c.Py_DecRef(l);
                                allocator.free(included_tmpl.source_path);
                                allocator.destroy(included_tmpl);
                            }
                        }
                    } else {
                        c.PyErr_Clear();
                    }
                }
            },
            // Recurse into container nodes to find nested includes
            .if_block => {
                for (node.if_branches) |*branch| {
                    resolveIncludes(branch.body, loader);
                }
            },
            .for_block => {
                resolveIncludes(node.for_body, loader);
                resolveIncludes(node.for_empty, loader);
            },
            .block_def, .with_block, .autoescape_block, .trans_block => {
                resolveIncludes(node.children, loader);
            },
            .dynamic_include => {}, // Resolved at render time, not compile time
            else => {},
        }
    }
}

// ── Renderer ─────────────────────────────────────────────────────────────────

/// Growable output buffer — each instance owns its own memory.
/// Starts with a small heap allocation, grows dynamically. No shared static buffers.
/// Safe for nested/recursive use (macros inside macros, nested for loops, etc.)
const OutputBuffer = struct {
    buf: []u8,
    pos: usize,

    /// Create a new output buffer with initial capacity.
    fn init() OutputBuffer {
        const initial = allocator.alloc(u8, 4096) catch return .{ .buf = &.{}, .pos = 0 };
        return .{ .buf = initial, .pos = 0 };
    }

    /// Create with specific initial capacity (for small nested renders like macros).
    fn initWithCapacity(cap: usize) OutputBuffer {
        const buf = allocator.alloc(u8, cap) catch return .{ .buf = &.{}, .pos = 0 };
        return .{ .buf = buf, .pos = 0 };
    }

    fn write(self: *OutputBuffer, data: []const u8) void {
        if (data.len == 0) return;
        self.ensureCapacity(data.len);
        if (self.pos + data.len > self.buf.len) return; // allocation failed
        @memcpy(self.buf[self.pos..][0..data.len], data);
        self.pos += data.len;
    }

    fn writeByte(self: *OutputBuffer, byte: u8) void {
        self.ensureCapacity(1);
        if (self.pos >= self.buf.len) return;
        self.buf[self.pos] = byte;
        self.pos += 1;
    }

    fn ensureCapacity(self: *OutputBuffer, needed: usize) void {
        if (self.pos + needed <= self.buf.len) return;
        const new_size = @max(self.buf.len * 2, self.pos + needed + 1024);
        const new_buf = allocator.alloc(u8, new_size) catch return;
        if (self.pos > 0) @memcpy(new_buf[0..self.pos], self.buf[0..self.pos]);
        if (self.buf.len > 0) allocator.free(self.buf);
        self.buf = new_buf;
    }

    fn result(self: *const OutputBuffer) []const u8 {
        return self.buf[0..self.pos];
    }

    fn deinit(self: *OutputBuffer) void {
        if (self.buf.len > 0) allocator.free(self.buf);
    }
};

pub fn render(tmpl: *const CompiledTemplate, context: *c.PyObject) ?*c.PyObject {
    var out = OutputBuffer.init();
    defer out.deinit();

    // Set current template for macro resolution
    const prev = current_template;
    current_template = tmpl;
    defer current_template = prev;

    // ── Dynamic extends: resolve parent at render time ──────────────────
    if (tmpl.dynamic_extends_expr) |dyn_expr| {
        const path_obj = evalExpr(dyn_expr, context);
        if (path_obj) |pobj| {
            defer c.Py_DecRef(pobj);
            if (c.PyUnicode_Check(pobj) != 0) {
                if (c.PyUnicode_AsUTF8(pobj)) |path_cstr| {
                    const parent_path = std.mem.span(path_cstr);
                    if (template_loader) |loader| {
                        const py_path = py.newString(parent_path) orelse return py.newBytes(out.result());
                        defer c.Py_DecRef(py_path);
                        const py_args = c.PyTuple_Pack(1, py_path) orelse return py.newBytes(out.result());
                        defer c.Py_DecRef(py_args);
                        const py_result = c.PyObject_CallObject(loader, py_args);
                        if (py_result) |src_obj| {
                            defer c.Py_DecRef(src_obj);
                            if (c.PyUnicode_Check(src_obj) != 0) {
                                if (c.PyUnicode_AsUTF8(src_obj)) |src| {
                                    const parent_source = std.mem.span(src);
                                    // Compile parent and merge child blocks
                                    const parent_tmpl = compile(parent_source, parent_path) catch return py.newBytes(out.result());

                                    // Merge child blocks into parent (same algorithm as static extends)
                                    var child_block_names: std.StringHashMapUnmanaged(void) = .{};
                                    if (tmpl.dynamic_extends_child_nodes) |child_nodes| {
                                        for (child_nodes) |cnode| {
                                            if (cnode.type == .block_def and cnode.block_name.len > 0) {
                                                child_block_names.put(allocator, cnode.block_name, {}) catch {};
                                                if (parent_tmpl.blocks.get(cnode.block_name)) |parent_node| {
                                                    parent_node.super_children = deepCloneNodes(parent_node.children);
                                                    parent_node.children = cnode.children;
                                                }
                                            }
                                        }
                                    }

                                    // Validate required blocks at render time
                                    var req_iter = parent_tmpl.blocks.iterator();
                                    while (req_iter.next()) |bentry| {
                                        const pnode = bentry.value_ptr.*;
                                        if (std.mem.eql(u8, pnode.text, "required")) {
                                            if (!child_block_names.contains(bentry.key_ptr.*)) {
                                                py.setError("Required block '{s}' not overridden", .{bentry.key_ptr.*});
                                                // F13(e): the merge loop above pointed each overridden
                                                // parent block's `children` at child-owned nodes
                                                // (parent_node.children = cnode.children). Restore them
                                                // to empty BEFORE parent_tmpl.deinit(), exactly as the
                                                // success path does — otherwise deinit frees nodes still
                                                // owned by tmpl.dynamic_extends_child_nodes, and the child
                                                // template frees them again later (double-free / UAF).
                                                if (tmpl.dynamic_extends_child_nodes) |cnodes| {
                                                    for (cnodes) |cn| {
                                                        if (cn.type == .block_def and cn.block_name.len > 0) {
                                                            if (parent_tmpl.blocks.get(cn.block_name)) |pn| {
                                                                pn.children = &.{};
                                                            }
                                                        }
                                                    }
                                                }
                                                parent_tmpl.deinit();
                                                allocator.destroy(parent_tmpl);
                                                child_block_names.deinit(allocator);
                                                return py.newBytes(out.result());
                                            }
                                        }
                                    }
                                    child_block_names.deinit(allocator);

                                    // Render the merged parent
                                    const prev2 = current_template;
                                    current_template = parent_tmpl;
                                    _ = renderNodes(parent_tmpl.nodes, context, &out, 0, null);
                                    current_template = prev2;

                                    // Free parent template — first null out aliased child block
                                    // children (they're owned by tmpl.dynamic_extends_child_nodes)
                                    if (tmpl.dynamic_extends_child_nodes) |child_nodes| {
                                        for (child_nodes) |cnode| {
                                            if (cnode.type == .block_def and cnode.block_name.len > 0) {
                                                if (parent_tmpl.blocks.get(cnode.block_name)) |parent_node| {
                                                    // Restore parent block children to empty — child data not owned by parent
                                                    parent_node.children = &.{};
                                                }
                                            }
                                        }
                                    }
                                    parent_tmpl.deinit();
                                    allocator.destroy(parent_tmpl);

                                    return py.newBytes(out.result());
                                }
                            }
                        } else {
                            c.PyErr_Clear();
                        }
                    }
                }
            }
        }
        // If expression evaluation failed, fall through to render child nodes directly
    }

    _ = renderNodes(tmpl.nodes, context, &out, 0, null);

    // If a Python exception was set (e.g., StrictUndefined), propagate it
    if (c.PyErr_Occurred() != null) return null;

    return py.newBytes(out.result());
}

// Thread-local template pointer for macro resolution during render.
// F13(d): MUST be `threadlocal` (like every neighbor below). As a plain global
// under free-threaded 3.14t, concurrent renders on different threads stomp each
// other's template pointer — cross-thread macro/block corruption and UAF.
threadlocal var current_template: ?*const CompiledTemplate = null;
threadlocal var autoescape_enabled: bool = true;

/// F13(c): PyDict_SetItemString INCREFs `val` and does NOT steal our reference.
/// py.newInt / py.pyTrue / py.pyFalse each hand back a NEW reference, so unless
/// we DecRef ours after inserting, every per-iteration loop-var set leaks an
/// object. This helper inserts and then drops our reference (steal semantics),
/// tolerating a null val (allocation failure).
fn dictSetSteal(dict: *c.PyObject, key: [*:0]const u8, val: ?*c.PyObject) void {
    if (val) |v| {
        _ = c.PyDict_SetItemString(dict, key, v);
        c.Py_DecRef(v);
    }
}

/// Undefined variable behavior: silent (default), strict (raise error), debug (show name)
const UndefinedMode = enum(u8) { silent = 0, strict = 1, debug = 2 };
threadlocal var undefined_mode: UndefinedMode = .silent;

/// Sandbox mode — restricts access to dangerous attributes
threadlocal var sandbox_enabled: bool = false;
threadlocal var i18n_callback: ?*c.PyObject = null;

/// Check if an attribute name is blocked in sandbox mode
fn isSandboxBlocked(attr: []const u8) bool {
    if (!sandbox_enabled) return false;
    // Block all dunder attributes except safe ones
    if (attr.len >= 4 and attr[0] == '_' and attr[1] == '_') {
        // Allow safe dunders
        if (std.mem.eql(u8, attr, "__len__")) return false;
        if (std.mem.eql(u8, attr, "__iter__")) return false;
        if (std.mem.eql(u8, attr, "__getitem__")) return false;
        if (std.mem.eql(u8, attr, "__contains__")) return false;
        if (std.mem.eql(u8, attr, "__str__")) return false;
        if (std.mem.eql(u8, attr, "__repr__")) return false;
        if (std.mem.eql(u8, attr, "__bool__")) return false;
        if (std.mem.eql(u8, attr, "__int__")) return false;
        if (std.mem.eql(u8, attr, "__float__")) return false;
        // Block everything else: __class__, __subclasses__, __globals__, __builtins__,
        // __mro__, __bases__, __init__, __dict__, __module__, __import__, etc.
        return true;
    }
    // Frame / generator / coroutine / async-gen / traceback / code internals reach
    // builtins and globals WITHOUT any dunder — e.g. a coroutine or generator in
    // the context escapes via `.cr_frame.f_builtins` / `.gi_frame.f_back...`. These
    // prefixes are exclusively Python-internal namespaces, so blocking the whole
    // prefix is safe and catches f_back/tb_next/cr_frame/ag_frame/co_names/… too.
    if (std.mem.startsWith(u8, attr, "gi_")) return true; // generator
    if (std.mem.startsWith(u8, attr, "cr_")) return true; // coroutine
    if (std.mem.startsWith(u8, attr, "ag_")) return true; // async generator
    if (std.mem.startsWith(u8, attr, "tb_")) return true; // traceback
    if (std.mem.startsWith(u8, attr, "co_")) return true; // code object
    if (std.mem.startsWith(u8, attr, "func_")) return true; // py2-style function attrs
    // Frame object accessors (f_globals/f_locals already blocked by name below).
    if (std.mem.eql(u8, attr, "f_builtins")) return true;
    if (std.mem.eql(u8, attr, "f_back")) return true;
    if (std.mem.eql(u8, attr, "f_code")) return true;
    if (std.mem.eql(u8, attr, "f_trace")) return true;
    if (std.mem.eql(u8, attr, "f_locals")) return true;
    if (std.mem.eql(u8, attr, "f_globals")) return true;
    return false;
}

/// Configurable template delimiters (defaults match Jinja2)
const Delimiters = struct {
    block_start: []const u8, // {%
    block_end: []const u8, // %}
    var_start: []const u8, // {{
    var_end: []const u8, // }}
    comment_start: []const u8, // {#
    comment_end: []const u8, // #}
};
const DEFAULT_DELIMS = Delimiters{
    .block_start = "{%",
    .block_end = "%}",
    .var_start = "{{",
    .var_end = "}}",
    .comment_start = "{#",
    .comment_end = "#}",
};
threadlocal var custom_delimiters: ?Delimiters = null;

// Recursive for-loop state — stored in threadlocals for the loop() callable
const RecursiveForState = struct {
    for_body: []const CompiledNode,
    for_var: []const u8,
    is_unpacking: bool,
    out: *OutputBuffer,
    depth: usize,
    rec_depth: usize, // current recursion depth (1-based for loop.depth)
};
threadlocal var recursive_for_state: ?RecursiveForState = null;
threadlocal var recursive_for_parent_ctx: ?*c.PyObject = null;

/// Total renderNodes recursion depth for THIS render, accumulated across macro /
/// include / extends boundaries (where the per-call `depth` is reset to 0). Caps
/// unbounded native recursion (recursive macros / recursive includes) that the
/// per-tree depth guard cannot see. Reset to 0 at the top of each public render.
threadlocal var render_call_depth: usize = 0;

fn renderNodes(nodes: []const CompiledNode, context: *c.PyObject, out: *OutputBuffer, depth: usize, current_super: ?[]const CompiledNode) LoopControl {
    if (depth > 64) return .normal; // per-tree nesting guard (if/for blocks)

    // Total render-call guard: `depth` is RESET to 0 across macro / dynamic-include
    // / extends-parent boundaries (renderNodes(..., 0, ...)), so it does NOT bound
    // recursion that crosses those boundaries — e.g. a self-recursive macro
    // `{% macro f() %}{{ f() }}{% endmacro %}` recurses renderNodes→evalExpr→
    // renderNodes forever, overflowing the native stack (worker crash / DoS). This
    // thread-local counter accumulates across every boundary and caps the total
    // native recursion. Sequential calls (loop iterations) don't accumulate — only
    // genuine nesting does — so legitimate deep templates are unaffected.
    if (render_call_depth >= 250) return .normal;
    render_call_depth += 1;
    defer render_call_depth -= 1;

    for (nodes) |*node| {
        switch (node.type) {
            .text => out.write(node.text),
            .variable => renderVariable(node, context, out),
            .if_block => {
                const ctl = renderIf(node, context, out, depth);
                if (ctl != .normal) return ctl;
            },
            .for_block => renderFor(node, context, out, depth),
            .block_def => _ = renderNodes(node.children, context, out, depth + 1, node.super_children),
            .set_var => renderSetVar(node, context),
            .include => {
                const inc_ctx = prepareIncludeContext(node, context);
                const owns_inc = inc_ctx != context;
                defer if (owns_inc) c.Py_DecRef(inc_ctx);
                _ = renderNodes(node.children, inc_ctx, out, depth + 1, null);
            },
            .dynamic_include => renderDynamicInclude(node, context, out, depth),
            .macro_def => {},
            .macro_call => renderMacroCall(node, context, out, depth),
            .call_block => renderCallBlock(node, context, out, depth),
            .with_block => renderWithBlock(node, context, out, depth),
            .super_call => {
                // Render parent block content
                if (current_super) |sc| {
                    _ = renderNodes(sc, context, out, depth + 1, null);
                }
            },
            .autoescape_block => {
                const prev_ae = autoescape_enabled;
                autoescape_enabled = !std.mem.eql(u8, node.text, "false");
                _ = renderNodes(node.children, context, out, depth + 1, null);
                autoescape_enabled = prev_ae;
            },
            .break_stmt => return .break_loop,
            .continue_stmt => return .continue_loop,
            .do_stmt => {
                // Evaluate expression for side effects, discard result
                if (node.expr) |expr| {
                    const val = evalExpr(expr, context);
                    if (val) |v| c.Py_DecRef(v);
                }
            },
            .debug_stmt => renderDebug(context, out),
            .trans_block => renderTrans(node, context, out, depth),
        }
    }
    return .normal;
}

/// Render a dynamic include — resolve path from expression or fallback list at render time.
fn renderDynamicInclude(node: *const CompiledNode, context: *c.PyObject, out: *OutputBuffer, depth: usize) void {
    if (depth > 64) return;

    // Prepare context: handle "without context" and "with key=expr" bindings
    const render_ctx = prepareIncludeContext(node, context);
    const owns_ctx = render_ctx != context;
    defer if (owns_ctx) c.Py_DecRef(render_ctx);

    // Helper: load, compile, and render a template by path string
    const loadAndRender = struct {
        fn call(path: []const u8, ctx: *c.PyObject, output: *OutputBuffer, d: usize) bool {
            const loader = template_loader orelse return false;
            const py_path = py.newString(path) orelse return false;
            defer c.Py_DecRef(py_path);
            const py_args = c.PyTuple_Pack(1, py_path) orelse return false;
            defer c.Py_DecRef(py_args);
            const py_result = c.PyObject_CallObject(loader, py_args);
            if (py_result) |src_obj| {
                defer c.Py_DecRef(src_obj);
                if (c.PyUnicode_Check(src_obj) != 0) {
                    if (c.PyUnicode_AsUTF8(src_obj)) |src| {
                        const included = compile(std.mem.span(src), path) catch return false;
                        // F13(a): the whole compiled template was leaked on every
                        // dynamic-include render — free it once rendered.
                        defer {
                            included.deinit();
                            allocator.destroy(included);
                        }
                        _ = renderNodes(included.nodes, ctx, output, d + 1, null);
                        return true;
                    }
                }
                return false;
            } else {
                c.PyErr_Clear();
                return false;
            }
        }
    };

    if (node.expr) |expr| {
        // Dynamic variable path: {% include partial_var %}
        const path_obj = evalExpr(expr, context);
        if (path_obj) |pobj| {
            defer c.Py_DecRef(pobj);

            if (c.PyUnicode_Check(pobj) != 0) {
                // Single string path
                if (c.PyUnicode_AsUTF8(pobj)) |path_cstr| {
                    const path = std.mem.span(path_cstr);
                    if (!loadAndRender.call(path, render_ctx, out, depth) and !node.ignore_missing) {
                        // Template not found and not ignoring — render nothing
                    }
                }
            } else if (c.PyList_Check(pobj) != 0) {
                // Python list of paths: {% include template_list %}
                const list_len: usize = @intCast(c.PyList_Size(pobj));
                for (0..list_len) |li| {
                    const item = c.PyList_GetItem(pobj, @intCast(li));
                    if (item) |it| {
                        if (c.PyUnicode_Check(it) != 0) {
                            if (c.PyUnicode_AsUTF8(it)) |path_cstr| {
                                if (loadAndRender.call(std.mem.span(path_cstr), render_ctx, out, depth)) return;
                            }
                        }
                    }
                }
                // No template in list succeeded — ignore_missing handles silently
            }
        }
        // Variable not found: ignore if ignore_missing, else render nothing
    } else if (node.block_name.len > 0) {
        // Fallback list from {% include ["a.html", "b.html"] %} — comma-separated in block_name
        var iter = std.mem.splitScalar(u8, node.block_name, ',');
        while (iter.next()) |path| {
            if (path.len > 0) {
                if (loadAndRender.call(path, render_ctx, out, depth)) return;
            }
        }
        // No template succeeded — silently ignored (fallback lists always ignore missing)
    }
}

/// Returns true only if a func_call expression statically resolves to a template
/// macro (whose body is pre-rendered HTML and therefore must not be re-escaped).
/// Mirrors the two macro-dispatch checks in evalExpr's `.func_call` arm so the
/// is_safe classification cannot diverge from where the call is actually handled.
/// A plain Python callable (global/simple_tag) returns false → its output is escaped.
fn exprCallIsMacro(expr: *const Expr) bool {
    const tmpl = current_template orelse return false;
    const callee = expr.left orelse return false;
    // Simple macro call: greet(...) — callee is literal_var("greet")
    if (callee.type == .literal_var and callee.var_path.parts.len == 1) {
        return tmpl.macros.contains(callee.var_path.parts[0]);
    }
    // Namespace macro call: m.input_field(...) — callee is getattr_expr(literal_var("m"), "input_field")
    if (callee.type == .getattr_expr) {
        if (callee.left) |ns_expr| {
            if (ns_expr.type == .literal_var and ns_expr.var_path.parts.len == 1) {
                const prefixed = std.fmt.allocPrint(allocator, "{s}.{s}", .{ ns_expr.var_path.parts[0], callee.str_val }) catch return false;
                defer allocator.free(prefixed);
                return tmpl.macros.contains(prefixed);
            }
        }
    }
    return false;
}

fn renderVariable(node: *const CompiledNode, context: *c.PyObject, out: *OutputBuffer) void {
    // Use expression evaluator if this is a math/ternary/concat expression
    const maybe_value = if (node.expr) |expr|
        evalExpr(expr, context)
    else
        resolveVar(&node.var_path, context);
    if (maybe_value == null) {
        // Variable missing — check if any filter provides a default
        for (node.filters) |*filter| {
            if (filter.native_id == FILTER_DEFAULT) {
                if (filter.arg) |a| {
                    const default_val = py.newString(a) orelse return;
                    writeValue(default_val, out, true);
                    c.Py_DecRef(default_val);
                    return;
                }
            }
        }
        // Handle undefined mode
        switch (undefined_mode) {
            .strict => {
                // Build variable name for error message
                if (node.var_path.parts.len > 0) {
                    py.setError("UndefinedError: '{s}' is undefined", .{node.var_path.parts[0]});
                } else {
                    py.setError("UndefinedError: variable is undefined", .{});
                }
            },
            .debug => {
                // Render as "{{ variable_name }}" for debugging
                out.write("{{ ");
                if (node.var_path.parts.len > 0) {
                    for (node.var_path.parts, 0..) |part, pi| {
                        if (pi > 0) out.write(".");
                        out.write(part);
                    }
                } else {
                    out.write("undefined");
                }
                out.write(" }}");
            },
            .silent => {},
        }
        return;
    }
    var value = maybe_value.?;
    // maybe_value is an OWNED new ref from evalExpr/resolveVar (sibling renderIf
    // decrefs the same evalExpr result). Start needs_decref=true so the no-filter
    // path also releases it. The filter loop below decrefs the prior value before
    // replacing, so there is exactly one decref on every path (immortal
    // True/False/None make a redundant decref harmless).
    var needs_decref = true;

    // Macro calls return pre-rendered HTML — don't double-escape. But ONLY a genuine
    // macro invocation is trusted: a plain global / simple_tag / func call returning
    // user data must still be escaped (otherwise `{{ f(x) }}` is an XSS sink). Values
    // that are legitimately safe (mark_safe / SafeString / Markup) are honored via the
    // __html__ protocol in writeValue, not via this heuristic.
    var is_safe = false;
    // Old-style macro call embedded in the var path, e.g. "greet(name)".
    if (node.var_path.parts.len > 0) {
        if (std.mem.indexOf(u8, node.var_path.parts[0], "(")) |paren| {
            const callee_name = node.var_path.parts[0][0..paren];
            if (current_template) |tmpl| {
                if (tmpl.macros.contains(callee_name)) is_safe = true;
            }
        }
    }
    // Expression-based func_call — safe only if it resolves to a macro (simple
    // `greet(...)` or namespace `m.input_field(...)`), never a plain callable.
    if (node.expr) |expr| {
        if (expr.type == .func_call and exprCallIsMacro(expr)) is_safe = true;
    }
    for (node.filters) |*filter| {
        const new_val = applyFilter(filter, value, context);
        if (new_val) |nv| {
            if (needs_decref) c.Py_DecRef(value);
            value = nv;
            needs_decref = true;
            // NOTE: FILTER_LIST is deliberately NOT here — a list repr contains the
            // unescaped str() of its elements and must be HTML-escaped like any value.
            if (filter.native_id == FILTER_SAFE or filter.native_id == FILTER_XMLATTR or filter.native_id == FILTER_URLIZE or filter.native_id == FILTER_TOJSON) is_safe = true;
        }
    }
    defer if (needs_decref) c.Py_DecRef(value);

    // Convert to string and write (auto_escape respects both |safe filter and {% autoescape %} block)
    writeValue(value, out, !is_safe and autoescape_enabled);
}

fn resolveVar(path: *const VarPath, context: *c.PyObject) ?*c.PyObject {
    if (path.parts.len == 0) return null;

    // Check for string literal
    const first = path.parts[0];
    if (first.len >= 2 and (first[0] == '\'' or first[0] == '"') and first[first.len - 1] == first[0]) {
        // Return string literal
        return py.newString(first[1 .. first.len - 1]);
    }

    // Check for numeric literal
    if (first.len > 0 and (first[0] >= '0' and first[0] <= '9')) {
        const val = std.fmt.parseInt(i64, first, 10) catch {
            // Try float
            const f = std.fmt.parseFloat(f64, first) catch return null;
            return c.PyFloat_FromDouble(f);
        };
        return c.PyLong_FromLongLong(val);
    }

    // Check for True/False/None
    if (std.mem.eql(u8, first, "True")) return py.pyTrue();
    if (std.mem.eql(u8, first, "False")) return py.pyFalse();
    if (std.mem.eql(u8, first, "None")) return py.pyNone();

    // Dict/attr/subscript/method lookup chain
    var obj: *c.PyObject = context;
    for (path.parts, 0..) |part, i| {
        // Check for macro call: "name(args)" at the root level (i == 0)
        if (i == 0 and std.mem.indexOf(u8, part, "(") != null) {
            if (current_template) |tmpl| {
                const paren = std.mem.indexOf(u8, part, "(").?;
                const macro_name = part[0..paren];
                if (tmpl.macros.get(macro_name)) |macro_idx| {
                    if (macro_idx < tmpl.nodes.len) {
                        // It's a macro call — parse args and render inline
                        const macro_node = &tmpl.nodes[macro_idx];
                        const close = std.mem.lastIndexOf(u8, part, ")") orelse part.len;
                        const args_str = part[paren + 1 .. close];
                        // Build a temporary macro_call node
                        var args: std.ArrayListUnmanaged([]const u8) = .empty;
                        if (args_str.len > 0) {
                            var it = std.mem.splitScalar(u8, args_str, ',');
                            while (it.next()) |a| {
                                const trimmed = std.mem.trim(u8, a, " ");
                                if (trimmed.len > 0) args.append(allocator, trimmed) catch continue;
                            }
                        }
                        var temp_node = CompiledNode{
                            .type = .macro_call,
                            .text = "",
                            .var_path = .{ .parts = &.{} },
                            .filters = &.{},
                            .if_branches = &.{},
                            .for_var = "",
                            .for_iter = .{ .parts = &.{} },
                            .for_iter_filters = &.{},
                            .for_body = &.{},
                            .for_empty = &.{},
                            .children = &.{},
                            .block_name = macro_name,
                            .set_name = "",
                            .macro_params = macro_node.macro_params,
                            .macro_args = args.items,
                            .expr = null,
                        };
                        // Render macro to a temporary buffer, return as Python string
                        var macro_out = OutputBuffer.init();
                        defer macro_out.deinit();
                        renderMacroCall(&temp_node, obj, &macro_out, 0);
                        args.deinit(allocator);
                        return py.newString(macro_out.result());
                    }
                }
            }
        }

        // Check for method call: "upper()" or "count(x)"
        if (std.mem.endsWith(u8, part, "()")) {
            const method_name = part[0 .. part.len - 2];
            // Sandbox check on the method name (not the full "name()" string)
            if (i > 0 and isSandboxBlocked(method_name)) {
                if (i > 0) c.Py_DecRef(obj);
                return null;
            }
            const method_name_z = allocator.dupeZ(u8, method_name) catch return null;
            defer allocator.free(method_name_z);
            const result = c.PyObject_CallMethod(obj, method_name_z.ptr, null);
            if (i > 0) c.Py_DecRef(obj);
            if (result == null) {
                c.PyErr_Clear();
                return null;
            }
            obj = result;
            continue;
        }

        // Check for subscript: "items[0]", "dict['key']", or chained "matrix[0][1]"
        if (std.mem.indexOf(u8, part, "[")) |first_bracket| {
            // First resolve the base name (needs null termination for C API)
            const base_name = part[0..first_bracket];
            if (base_name.len > 0) {
                const base_key_z = allocator.dupeZ(u8, base_name) catch return null;
                defer allocator.free(base_key_z);
                const base = c.PyMapping_GetItemString(obj, base_key_z.ptr) orelse {
                    c.PyErr_Clear();
                    if (i > 0) c.Py_DecRef(obj);
                    return null;
                };
                if (i > 0) c.Py_DecRef(obj);
                obj = base;
            }

            // Apply all subscripts in sequence: [0], [1], ['key'], etc.
            var scan_pos = first_bracket;
            while (scan_pos < part.len and part[scan_pos] == '[') {
                const close = std.mem.indexOfPos(u8, part, scan_pos, "]") orelse break;
                var index_str = part[scan_pos + 1 .. close];
                // Strip quotes from string subscripts
                if (index_str.len >= 2 and (index_str[0] == '\'' or index_str[0] == '"')) {
                    index_str = index_str[1 .. index_str.len - 1];
                }

                // Try integer index first
                if (std.fmt.parseInt(c.Py_ssize_t, index_str, 10)) |idx| {
                    const item = c.PySequence_GetItem(obj, idx);
                    c.Py_DecRef(obj);
                    if (item == null) {
                        c.PyErr_Clear();
                        return null;
                    }
                    obj = item;
                } else |_| {
                    const key = py.newString(index_str) orelse {
                        c.Py_DecRef(obj);
                        return null;
                    };
                    defer c.Py_DecRef(key);
                    const item = c.PyObject_GetItem(obj, key);
                    c.Py_DecRef(obj);
                    if (item == null) {
                        c.PyErr_Clear();
                        return null;
                    }
                    obj = item;
                }

                scan_pos = close + 1;
            }
            continue;
        }

        // Standard dict/attr lookup. `part` is null-terminated at
        // compile time (VarPath.parts is [:0]const u8) so we pass
        // `part.ptr` directly to the C API — no per-access allocator
        // round-trip. Measured hot path on template-heavy renders.
        //
        // Fast-path: for dict contexts (the common root case AND any
        // intermediate dict), use PyDict_GetItemString which does NOT
        // set an exception on miss — saves the PyErr_Clear() + fall
        // through when keys are truly attributes on a Model instance.
        var next: ?*c.PyObject = null;
        if (c.PyDict_Check(obj) != 0) {
            const borrowed = c.PyDict_GetItemString(obj, part.ptr);
            if (borrowed) |b| {
                c.Py_IncRef(b);
                next = b;
            }
        } else {
            next = c.PyMapping_GetItemString(obj, part.ptr);
            if (next == null) c.PyErr_Clear();
        }
        if (next == null) {
            // Sandbox: block dangerous attribute access before getattr
            if (i > 0 and isSandboxBlocked(part)) {
                if (i > 0) c.Py_DecRef(obj);
                return null;
            }
            // Try getattr as fallback (handles Model attributes).
            // PyUnicode_FromStringAndSize skips the strlen() that
            // py.newString's PyUnicode_FromString would do — the
            // length is already known from the slice.
            const attr_name = c.PyUnicode_FromStringAndSize(part.ptr, @intCast(part.len)) orelse return null;
            defer c.Py_DecRef(attr_name);
            const attr_val = c.PyObject_GetAttr(obj, attr_name);
            if (attr_val == null) {
                c.PyErr_Clear();
                if (i > 0) c.Py_DecRef(obj);
                return null;
            }
            if (i > 0) c.Py_DecRef(obj);
            obj = attr_val.?;
        } else {
            if (i > 0) c.Py_DecRef(obj);
            obj = next.?;
        }
    }

    return obj;
}

fn writeValue(value: *c.PyObject, out: *OutputBuffer, auto_escape: bool) void {
    // None → empty string
    if (value == @as(*c.PyObject, @ptrCast(&c._Py_NoneStruct))) return;

    // Bool
    if (c.PyBool_Check(value) != 0) {
        if (value == py.pyTrue()) out.write("True") else out.write("False");
        return;
    }

    // Int. Python integers are arbitrary precision; a C long is not. Values
    // outside its range (snowflake ids, hashes, 2**64 counters) must render
    // their real digits, so overflow falls through to PyObject_Str below
    // rather than writing a truncated -1. The error PyLong_AsLong set on
    // overflow MUST be cleared here: leaving it pending makes the next
    // unrelated CPython call fail, which surfaced as an OverflowError
    // escaping the renderer entirely — every value >= 2**63 crashed the
    // render, filter or not.
    if (c.PyLong_Check(value) != 0) {
        const val = c.PyLong_AsLong(value);
        if (val != -1 or c.PyErr_Occurred() == null) {
            var num_buf: [24]u8 = undefined;
            const s = std.fmt.bufPrint(&num_buf, "{d}", .{val}) catch return;
            out.write(s);
            return;
        }
        c.PyErr_Clear();
    }

    // Float
    if (c.PyFloat_Check(value) != 0) {
        const val = c.PyFloat_AsDouble(value);
        var num_buf: [32]u8 = undefined;
        const s = std.fmt.bufPrint(&num_buf, "{d}", .{val}) catch return;
        out.write(s);
        return;
    }

    // Honor the __html__ protocol (Django SafeString / mark_safe, markupsafe.Markup):
    // such values are already-safe HTML and must NOT be re-escaped. This is only
    // consulted on the escaping path, and exact `str` is fast-pathed away (a plain
    // str never carries __html__), so only str *subclasses* and non-str objects pay
    // the attribute probe. When auto_escape is false the raw str() form is written
    // anyway, so __html__ (whose result equals str() for SafeString/Markup) is moot.
    if (auto_escape and c.PyUnicode_CheckExact(value) == 0) {
        if (c.PyObject_HasAttrString(value, "__html__") != 0) {
            if (c.PyObject_CallMethod(value, "__html__", null)) |html| {
                defer c.Py_DecRef(html);
                var html_len: c.Py_ssize_t = 0;
                if (c.PyUnicode_AsUTF8AndSize(html, &html_len)) |hp| {
                    out.write(hp[0..@intCast(html_len)]);
                    return;
                }
            }
            c.PyErr_Clear();
        }
    }

    // String
    var str_len: c.Py_ssize_t = 0;
    const str_ptr = if (c.PyUnicode_Check(value) != 0)
        c.PyUnicode_AsUTF8AndSize(value, &str_len)
    else blk: {
        // Convert to string
        const str_obj = c.PyObject_Str(value) orelse return;
        defer c.Py_DecRef(str_obj);
        break :blk c.PyUnicode_AsUTF8AndSize(str_obj, &str_len);
    };

    if (str_ptr) |sp| {
        const data = sp[0..@intCast(str_len)];
        if (auto_escape) {
            // HTML escape using SIMD
            htmlEscapeWrite(data, out);
        } else {
            out.write(data);
        }
    }
}

fn htmlEscapeWrite(data: []const u8, out: *OutputBuffer) void {
    var start: usize = 0;
    for (data, 0..) |ch, i| {
        const replacement: ?[]const u8 = switch (ch) {
            '&' => "&amp;",
            '<' => "&lt;",
            '>' => "&gt;",
            '"' => "&quot;",
            '\'' => "&#x27;",
            else => null,
        };
        if (replacement) |r| {
            if (i > start) out.write(data[start..i]);
            out.write(r);
            start = i + 1;
        }
    }
    if (start < data.len) out.write(data[start..]);
}

/// Append `data` to an ArrayList, HTML-escaping & < > " ' so it is safe both in
/// element text and inside a double-/single-quoted attribute value.
fn htmlEscapeAppend(buf: *std.ArrayListUnmanaged(u8), data: []const u8) void {
    for (data) |ch| {
        switch (ch) {
            '&' => buf.appendSlice(allocator, "&amp;") catch {},
            '<' => buf.appendSlice(allocator, "&lt;") catch {},
            '>' => buf.appendSlice(allocator, "&gt;") catch {},
            '"' => buf.appendSlice(allocator, "&quot;") catch {},
            '\'' => buf.appendSlice(allocator, "&#x27;") catch {},
            else => buf.append(allocator, ch) catch {},
        }
    }
}

fn renderIf(node: *const CompiledNode, context: *c.PyObject, out: *OutputBuffer, depth: usize) LoopControl {
    for (node.if_branches) |*branch| {
        if (branch.condition_expr) |expr| {
            const val = evalExpr(expr, context);
            defer if (val) |v| c.Py_DecRef(v);

            const truth = if (val) |v| c.PyObject_IsTrue(v) == 1 else false;
            if (truth) {
                return renderNodes(branch.body, context, out, depth + 1, null);
            }
        } else {
            // else branch — always true
            return renderNodes(branch.body, context, out, depth + 1, null);
        }
    }
    return .normal;
}

// ── loop.cycle() and loop.changed() C function implementations ──────────────

/// loop.cycle(*args) — returns args[index0 % len(args)]
/// self is the loop_dict, args is the Python *args tuple
fn loopCycleImpl(self_obj: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    const loop_dict = self_obj orelse return py.pyNone();
    const idx_obj = c.PyDict_GetItemString(loop_dict, "index0") orelse return py.pyNone();
    const idx = c.PyLong_AsLong(idx_obj);
    if (idx < 0) return py.pyNone();

    const arg_tuple = args orelse return py.pyNone();
    const nargs = c.PyTuple_Size(arg_tuple);
    if (nargs <= 0) return py.pyNone();

    const pick = @mod(idx, nargs);
    const result = c.PyTuple_GetItem(arg_tuple, pick) orelse return py.pyNone();
    c.Py_IncRef(result);
    return result;
}

/// loop.changed(val) — returns True if val != previous val for this slot
fn loopChangedImpl(self_obj: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    const loop_dict = self_obj orelse return py.pyTrue();
    const arg_tuple = args orelse return py.pyTrue();
    if (c.PyTuple_Size(arg_tuple) < 1) return py.pyTrue();

    const val = c.PyTuple_GetItem(arg_tuple, 0) orelse return py.pyTrue();
    const prev = c.PyDict_GetItemString(loop_dict, "_changed_prev");

    const changed = if (prev) |p|
        c.PyObject_RichCompareBool(val, p, c.Py_EQ) != 1
    else
        true;

    _ = c.PyDict_SetItemString(loop_dict, "_changed_prev", val);
    return if (changed) py.pyTrue() else py.pyFalse();
}

var cycle_method_def = c.PyMethodDef{
    .ml_name = "cycle",
    .ml_meth = @ptrCast(&loopCycleImpl),
    .ml_flags = c.METH_VARARGS,
    .ml_doc = "cycle(*args) — cycle through values by loop index",
};

var changed_method_def = c.PyMethodDef{
    .ml_name = "changed",
    .ml_meth = @ptrCast(&loopChangedImpl),
    .ml_flags = c.METH_VARARGS,
    .ml_doc = "changed(val) — True if val changed since last call",
};

/// loop(iterable) — recursive for-loop re-entry. Called from templates as {{ loop(item.children) }}.
/// Renders the for-body with the given iterable, incrementing loop.depth.
fn loopRecurseImpl(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    const arg_tuple = args orelse return py.newString("");
    if (c.PyTuple_Size(arg_tuple) < 1) return py.newString("");
    const new_iterable = c.PyTuple_GetItem(arg_tuple, 0) orelse return py.newString("");

    const state = recursive_for_state orelse return py.newString("");

    // Save and increment recursion depth
    const prev_state = recursive_for_state;
    var new_state = state;
    new_state.rec_depth = state.rec_depth + 1;
    recursive_for_state = new_state;
    defer recursive_for_state = prev_state;

    // Run the for-loop iteration on the new iterable
    renderForIterable(new_iterable, state.for_body, state.for_var, state.is_unpacking, state.out, state.depth, new_state.rec_depth);

    return py.newString("");
}

var recurse_method_def = c.PyMethodDef{
    .ml_name = "loop_recurse",
    .ml_meth = @ptrCast(&loopRecurseImpl),
    .ml_flags = c.METH_VARARGS,
    .ml_doc = "loop(iterable) — recursively render for-loop body",
};

fn renderFor(node: *const CompiledNode, context: *c.PyObject, out: *OutputBuffer, depth: usize) void {
    // Resolve the iterable
    var iterable = if (node.expr) |expr|
        (evalExpr(expr, context) orelse {
            _ = renderNodes(node.for_empty, context, out, depth + 1, null);
            return;
        })
    else
        (resolveVar(&node.for_iter, context) orelse {
            _ = renderNodes(node.for_empty, context, out, depth + 1, null);
            return;
        });
    defer c.Py_DecRef(iterable);

    // Apply filters to iterable
    for (node.filters) |*filter| {
        const new_val = applyFilter(filter, iterable, context);
        if (new_val) |nv| {
            c.Py_DecRef(iterable);
            iterable = nv;
        }
    }

    const length = c.PyObject_Length(iterable);
    if (length <= 0) {
        _ = renderNodes(node.for_empty, context, out, depth + 1, null);
        return;
    }

    const is_unpacking = std.mem.indexOf(u8, node.for_var, ",") != null;
    const is_recursive = std.mem.eql(u8, node.text, "recursive");

    // Set parent context for scoped loop body
    const prev_parent_ctx = recursive_for_parent_ctx;
    recursive_for_parent_ctx = context;
    defer recursive_for_parent_ctx = prev_parent_ctx;

    // For recursive loops: set up threadlocal state for loop() callable
    if (is_recursive) {
        const prev_state = recursive_for_state;
        recursive_for_state = .{
            .for_body = node.for_body,
            .for_var = node.for_var,
            .is_unpacking = is_unpacking,
            .out = out,
            .depth = depth,
            .rec_depth = 1,
        };
        defer recursive_for_state = prev_state;
        renderForIterable(iterable, node.for_body, node.for_var, is_unpacking, out, depth, 1);
    } else {
        renderForIterable(iterable, node.for_body, node.for_var, is_unpacking, out, depth, 0);
    }
    c.PyErr_Clear();
}

/// Core for-loop iteration — shared between renderFor and recursive loop() calls.
/// rec_depth: 0 = non-recursive, 1+ = recursive depth level
fn renderForIterable(
    iterable: *c.PyObject,
    for_body: []const CompiledNode,
    for_var: []const u8,
    is_unpacking: bool,
    out: *OutputBuffer,
    depth: usize,
    rec_depth: usize,
) void {
    const length = c.PyObject_Length(iterable);
    if (length <= 0) return;

    // Parse unpack var names if needed
    var unpack_names: std.ArrayListUnmanaged([]const u8) = .empty;
    defer unpack_names.deinit(allocator);
    if (is_unpacking) {
        var it = std.mem.splitScalar(u8, for_var, ',');
        while (it.next()) |part| {
            const name = std.mem.trim(u8, part, " ");
            if (name.len > 0) unpack_names.append(allocator, name) catch {};
        }
    }

    const loop_dict = c.PyDict_New() orelse return;
    defer c.Py_DecRef(loop_dict);

    // Scoped context for loop body
    // For recursive re-entry, we still need a scoped context to prevent set leakage
    // Use a fresh dict based on the iterable's parent context
    const loop_ctx = c.PyDict_New() orelse return;
    defer c.Py_DecRef(loop_ctx);
    // Copy all context from the recursive state or create fresh
    if (recursive_for_state) |state| {
        _ = state; // we use the state for depth tracking
    }
    // For proper scoping, we need the parent context. Since renderForIterable
    // is called from renderFor (which has context) and from loopRecurseImpl
    // (which doesn't pass context), we need to reconstruct it.
    // Simplification: use a global threadlocal for the parent context.
    if (recursive_for_parent_ctx) |parent| {
        _ = c.PyDict_Update(loop_ctx, parent);
    }

    const iter = c.PyObject_GetIter(iterable) orelse return;
    defer c.Py_DecRef(iter);

    var index: c.Py_ssize_t = 0;
    while (c.PyIter_Next(iter)) |item| {
        defer c.Py_DecRef(item);

        if (is_unpacking) {
            for (unpack_names.items, 0..) |name, ui| {
                const sub_item = c.PySequence_GetItem(item, @intCast(ui));
                if (sub_item) |si| {
                    const name_z = allocator.dupeZ(u8, name) catch continue;
                    defer allocator.free(name_z);
                    _ = c.PyMapping_SetItemString(loop_ctx, name_z.ptr, si);
                    c.Py_DecRef(si);
                } else {
                    c.PyErr_Clear();
                }
            }
        } else {
            const key_z = allocator.dupeZ(u8, for_var) catch return;
            defer allocator.free(key_z);
            _ = c.PyMapping_SetItemString(loop_ctx, key_z.ptr, item);
        }

        // Set loop dict — F13(c): each value is a NEW reference; dictSetSteal
        // inserts then drops our ref so these no longer leak per iteration.
        dictSetSteal(loop_dict, "index0", py.newInt(index));
        dictSetSteal(loop_dict, "index", py.newInt(index + 1));
        dictSetSteal(loop_dict, "revindex", py.newInt(length - index));
        dictSetSteal(loop_dict, "revindex0", py.newInt(length - index - 1));
        dictSetSteal(loop_dict, "first", if (index == 0) py.pyTrue() else py.pyFalse());
        dictSetSteal(loop_dict, "last", if (index == length - 1) py.pyTrue() else py.pyFalse());
        dictSetSteal(loop_dict, "length", py.newInt(length));

        // Recursive depth tracking
        if (rec_depth > 0) {
            dictSetSteal(loop_dict, "depth", py.newInt(@intCast(rec_depth)));
            dictSetSteal(loop_dict, "depth0", py.newInt(@intCast(rec_depth - 1)));
            // Add loop() callable for recursive re-entry
            const recurse_func = c.PyCFunction_New(&recurse_method_def, loop_dict);
            if (recurse_func) |rf| {
                _ = c.PyDict_SetItemString(loop_dict, "__call__", rf);
                c.Py_DecRef(rf);
            }
            // Also make loop itself callable (Jinja2: {{ loop(items) }})
            // We achieve this by setting loop as a special callable in the context
        }

        const cycle_func = c.PyCFunction_New(&cycle_method_def, loop_dict);
        if (cycle_func) |cf| {
            _ = c.PyDict_SetItemString(loop_dict, "cycle", cf);
            c.Py_DecRef(cf);
        }
        const changed_func = c.PyCFunction_New(&changed_method_def, loop_dict);
        if (changed_func) |chf| {
            _ = c.PyDict_SetItemString(loop_dict, "changed", chf);
            c.Py_DecRef(chf);
        }

        if (index > 0) {
            if (c.PyDict_GetItemString(loop_dict, "_prev")) |prev| {
                _ = c.PyDict_SetItemString(loop_dict, "previtem", prev);
            }
        }
        _ = c.PyDict_SetItemString(loop_dict, "_prev", item);

        // Set "loop" in context — always as a dict (for loop.index, loop.depth, etc.)
        // For recursive loops, the dict also has __call__ for loop(children) invocation
        _ = c.PyMapping_SetItemString(loop_ctx, "loop", loop_dict);

        const ctl = renderNodes(for_body, loop_ctx, out, depth + 1, null);
        if (ctl == .break_loop) break;
        index += 1;
    }
}

fn renderSetVar(node: *const CompiledNode, context: *c.PyObject) void {
    // Use expression evaluator if this is a computed expression
    const value = if (node.expr) |expr|
        evalExpr(expr, context)
    else
        resolveVar(&node.var_path, context);
    if (value == null) return;
    defer c.Py_DecRef(value.?);

    // Check for dot-path assignment: {% set ns.counter = expr %}
    if (std.mem.indexOf(u8, node.set_name, ".")) |dot_pos| {
        // Split on first dot: "ns" + "counter"
        const obj_name = node.set_name[0..dot_pos];
        const attr_name = node.set_name[dot_pos + 1 ..];
        const obj_key = allocator.dupeZ(u8, obj_name) catch return;
        defer allocator.free(obj_key);
        const attr_key = allocator.dupeZ(u8, attr_name) catch return;
        defer allocator.free(attr_key);
        // Resolve the object from context
        const obj = c.PyMapping_GetItemString(context, obj_key.ptr);
        if (obj) |o| {
            defer c.Py_DecRef(o);
            // Set attribute on the object
            _ = c.PyObject_SetAttrString(o, attr_key.ptr, value.?);
        }
    } else {
        const key_z = allocator.dupeZ(u8, node.set_name) catch return;
        defer allocator.free(key_z);
        _ = c.PyMapping_SetItemString(context, key_z.ptr, value.?);
    }
}

/// Prepare context for include: handles "without context" and "with key=expr" bindings.
/// Returns context (possibly new dict) — caller must check ownership and Py_DecRef.
fn prepareIncludeContext(node: *const CompiledNode, context: *c.PyObject) *c.PyObject {
    const is_without = std.mem.eql(u8, node.set_name, "without");
    const has_bindings = node.macro_params.len > 0;

    if (!is_without and !has_bindings) {
        // Default: pass parent context through
        return context;
    }

    // Start with empty dict (without context) or copy of parent context (with bindings)
    const ctx = if (is_without) (c.PyDict_New() orelse return context) else (c.PyDict_Copy(context) orelse return context);

    // Apply variable bindings: evaluate each expression against the PARENT context
    for (node.macro_params) |*param| {
        if (param.default_val) |val_str| {
            const parsed = parseExpressionFull(val_str) catch continue;
            var val = evalExpr(parsed.expr, context); // evaluate against parent, not new ctx
            parsed.expr.deinit();
            allocator.destroy(parsed.expr);

            // Apply extracted filters (e.g. name|upper → "upper" extracted by parseExpressionFull)
            if (val) |v| {
                var current_val = v;
                for (parsed.filters) |*f| {
                    if (applyFilter(f, current_val, context)) |filtered| {
                        c.Py_DecRef(current_val);
                        current_val = filtered;
                    }
                }
                val = current_val;
            }
            for (parsed.filters) |*f| {
                allocator.free(f.name);
                if (f.arg) |a| allocator.free(a);
            }
            if (parsed.filters.len > 0) allocator.free(parsed.filters);

            if (val) |v| {
                const key_z = allocator.dupeZ(u8, param.name) catch continue;
                defer allocator.free(key_z);
                _ = c.PyMapping_SetItemString(ctx, key_z.ptr, v);
                c.Py_DecRef(v);
            }
        }
    }
    return ctx;
}

fn renderWithBlock(node: *const CompiledNode, context: *c.PyObject, out: *OutputBuffer, depth: usize) void {
    // Create scoped context: copy current, set bindings, render body, restore
    const scoped_ctx = c.PyDict_Copy(context) orelse return;
    defer c.Py_DecRef(scoped_ctx);

    // Set variable bindings
    for (node.macro_params) |*param| {
        if (param.default_val) |val_str| {
            // Parse value expression and evaluate
            const parsed = parseExpressionFull(val_str) catch continue;
            var val = evalExpr(parsed.expr, context);
            // Free parsed expr
            parsed.expr.deinit();
            allocator.destroy(parsed.expr);

            // Apply extracted filters (e.g. x|upper → "upper" extracted by parseExpressionFull)
            if (val) |v| {
                var current_val = v;
                for (parsed.filters) |*f| {
                    if (applyFilter(f, current_val, context)) |filtered| {
                        c.Py_DecRef(current_val);
                        current_val = filtered;
                    }
                }
                val = current_val;
            }
            for (parsed.filters) |*f| {
                allocator.free(f.name);
                if (f.arg) |a| allocator.free(a);
            }
            if (parsed.filters.len > 0) allocator.free(parsed.filters);

            if (val) |v| {
                const key_z = allocator.dupeZ(u8, param.name) catch continue;
                defer allocator.free(key_z);
                _ = c.PyMapping_SetItemString(scoped_ctx, key_z.ptr, v);
                c.Py_DecRef(v);
            }
        }
    }

    _ = renderNodes(node.children, scoped_ctx, out, depth + 1, null);
}

/// Render {% debug %} — dump context variables as formatted text.
/// Outputs context dict repr to the output buffer.
fn renderDebug(context: *c.PyObject, out: *OutputBuffer) void {
    // Call repr(context) to get a formatted dump
    const repr_obj = c.PyObject_Repr(context) orelse return;
    defer c.Py_DecRef(repr_obj);
    var size: c.Py_ssize_t = 0;
    const ptr = c.PyUnicode_AsUTF8AndSize(repr_obj, &size);
    if (ptr) |p| {
        const str = p[0..@intCast(size)];
        out.write(str);
    }
}

/// Render {% trans %}...{% endtrans %} — i18n translation block.
/// Renders body to get the translation key string, then looks up translation
/// via the registered i18n callback. Variable bindings in macro_params are
/// evaluated and substituted into the translated string.
fn renderTrans(node: *const CompiledNode, context: *c.PyObject, out: *OutputBuffer, depth: usize) void {
    // Render body nodes to get the raw translation key
    var key_buf = OutputBuffer.init();
    // F13(b): OutputBuffer.init() allocates a ≥4KB backing buffer; without this
    // deinit every {% trans %} render leaked it. key_str is a view into key_buf,
    // used until this function returns, so deinit at scope end is safe.
    defer key_buf.deinit();
    _ = renderNodes(node.children, context, &key_buf, depth + 1, null);
    const key_str = key_buf.result();

    // Look up translation via Python callback (i18n_callback threadlocal)
    const translated = blk: {
        if (i18n_callback) |cb| {
            const py_key = py.newString(key_str) orelse break :blk key_str;
            defer c.Py_DecRef(py_key);
            const py_args = c.PyTuple_Pack(1, py_key) orelse break :blk key_str;
            defer c.Py_DecRef(py_args);
            const result = c.PyObject_CallObject(cb, py_args) orelse break :blk key_str;
            defer c.Py_DecRef(result);
            var size: c.Py_ssize_t = 0;
            const ptr = c.PyUnicode_AsUTF8AndSize(result, &size);
            if (ptr) |p| {
                const translated_str = allocator.dupe(u8, p[0..@intCast(size)]) catch break :blk key_str;
                break :blk translated_str;
            }
        }
        break :blk key_str;
    };
    const owns_translated = translated.ptr != key_str.ptr;

    // Apply variable bindings via simple string replacement: %(name)s → value
    if (node.macro_params.len > 0) {
        var result_str = translated;
        for (node.macro_params) |*param| {
            if (param.default_val) |val_str| {
                const parsed = parseExpressionFull(val_str) catch continue;
                var val = evalExpr(parsed.expr, context);
                parsed.expr.deinit();
                allocator.destroy(parsed.expr);
                if (val) |v| {
                    var current_val = v;
                    for (parsed.filters) |*f| {
                        if (applyFilter(f, current_val, context)) |filtered| {
                            c.Py_DecRef(current_val);
                            current_val = filtered;
                        }
                    }
                    val = current_val;
                }
                for (parsed.filters) |*f| {
                    allocator.free(f.name);
                    if (f.arg) |a| allocator.free(a);
                }
                if (parsed.filters.len > 0) allocator.free(parsed.filters);

                if (val) |v| {
                    // Get string repr of the value
                    const str_obj = c.PyObject_Str(v) orelse {
                        c.Py_DecRef(v);
                        continue;
                    };
                    c.Py_DecRef(v);
                    var val_size: c.Py_ssize_t = 0;
                    const val_ptr = c.PyUnicode_AsUTF8AndSize(str_obj, &val_size);
                    if (val_ptr) |vp| {
                        const val_str_slice = vp[0..@intCast(val_size)];
                        // Build placeholder: %(name)s
                        var placeholder_buf: [256]u8 = undefined;
                        const placeholder = std.fmt.bufPrint(&placeholder_buf, "%({s})s", .{param.name}) catch continue;
                        // Replace in result_str
                        const replaced = strReplace(result_str, placeholder, val_str_slice);
                        if (replaced.ptr != result_str.ptr) {
                            if (result_str.ptr != translated.ptr and result_str.ptr != key_str.ptr) {
                                allocator.free(result_str);
                            }
                            result_str = replaced;
                        }
                    }
                    c.Py_DecRef(str_obj);
                }
            }
        }
        out.write(result_str);
        if (result_str.ptr != translated.ptr and result_str.ptr != key_str.ptr) {
            allocator.free(result_str);
        }
    } else {
        out.write(translated);
    }

    if (owns_translated) allocator.free(translated);
}

/// Simple string replace helper for trans variable substitution.
fn strReplace(haystack: []const u8, needle: []const u8, replacement: []const u8) []const u8 {
    const pos = std.mem.indexOf(u8, haystack, needle) orelse return haystack;
    const result_len = haystack.len - needle.len + replacement.len;
    const result = allocator.alloc(u8, result_len) catch return haystack;
    @memcpy(result[0..pos], haystack[0..pos]);
    @memcpy(result[pos..][0..replacement.len], replacement);
    @memcpy(result[pos + replacement.len ..], haystack[pos + needle.len ..]);
    return result;
}

// ── Filter application ───────────────────────────────────────────────────────

/// Render a macro call: look up macro in template registry, bind args, render body
/// Resolve a macro argument value from its string representation.
/// Handles: 'string literal', "string literal", variable reference, integer
fn resolveArgValue(arg_str: []const u8, context: *c.PyObject) ?*c.PyObject {
    if (arg_str.len == 0) return py.newString("");

    // String literal (single or double quoted)
    if (arg_str.len >= 2 and (arg_str[0] == '\'' or arg_str[0] == '"')) {
        return py.newString(arg_str[1 .. arg_str.len - 1]);
    }

    // Integer literal
    if (arg_str[0] >= '0' and arg_str[0] <= '9') {
        const val = std.fmt.parseInt(i64, arg_str, 10) catch return py.newString(arg_str);
        return py.newInt(@intCast(val));
    }

    // Boolean/None
    if (std.mem.eql(u8, arg_str, "True") or std.mem.eql(u8, arg_str, "true")) return py.pyTrue();
    if (std.mem.eql(u8, arg_str, "False") or std.mem.eql(u8, arg_str, "false")) return py.pyFalse();
    if (std.mem.eql(u8, arg_str, "None") or std.mem.eql(u8, arg_str, "none")) return py.pyNone();

    // Variable reference
    const path = parseVarPath(arg_str) catch return py.newString(arg_str);
    const val = resolveVar(&path, context);
    freeVarPath(@constCast(&path));
    return val;
}

fn renderMacroCall(node: *const CompiledNode, context: *c.PyObject, out: *OutputBuffer, depth: usize) void {
    const tmpl = current_template orelse return;
    const macro_idx = tmpl.macros.get(node.block_name) orelse return;
    if (macro_idx >= tmpl.nodes.len) return;
    const macro_node = &tmpl.nodes[macro_idx];

    // Create new context scope with macro parameters bound
    const macro_ctx = c.PyDict_Copy(context) orelse return;
    defer c.Py_DecRef(macro_ctx);

    // First pass: collect keyword args from macro_args
    // Keyword args look like "name='value'" — contain '=' not inside quotes
    var kwarg_map: std.StringHashMapUnmanaged([]const u8) = .{};
    defer kwarg_map.deinit(allocator);
    var positional_args: std.ArrayListUnmanaged([]const u8) = .empty;
    defer positional_args.deinit(allocator);

    for (node.macro_args) |arg_str| {
        // Check for keyword arg pattern: name='value' or name=value
        const eq_pos = blk: {
            var in_quote: u8 = 0;
            for (arg_str, 0..) |ch, idx| {
                if (in_quote != 0) {
                    if (ch == in_quote) in_quote = 0;
                    continue;
                }
                if (ch == '\'' or ch == '"') {
                    in_quote = ch;
                    continue;
                }
                if (ch == '=' and idx > 0) break :blk idx;
            }
            break :blk @as(?usize, null);
        };

        if (eq_pos) |eqp| {
            const kw_name = std.mem.trim(u8, arg_str[0..eqp], " ");
            const kw_val = std.mem.trim(u8, arg_str[eqp + 1 ..], " ");
            kwarg_map.put(allocator, kw_name, kw_val) catch {};
        } else {
            positional_args.append(allocator, arg_str) catch {};
        }
    }

    // Second pass: bind params — positional first, then keyword, then defaults
    for (macro_node.macro_params, 0..) |*param, pi| {
        const param_key = allocator.dupeZ(u8, param.name) catch continue;
        defer allocator.free(param_key);

        var arg_val: ?*c.PyObject = null;

        // Try keyword arg first
        if (kwarg_map.get(param.name)) |kw_val| {
            arg_val = resolveArgValue(kw_val, context);
        } else if (pi < positional_args.items.len) {
            // Positional arg
            arg_val = resolveArgValue(positional_args.items[pi], context);
        } else if (param.default_val) |default| {
            // Default value
            arg_val = resolveArgValue(default, context);
        }

        if (arg_val) |v| {
            _ = c.PyDict_SetItemString(macro_ctx, param_key.ptr, v);
            c.Py_DecRef(v);
        }
    }

    _ = renderNodes(macro_node.children, macro_ctx, out, depth + 1, null);
}

/// Render {% call macro_name(args) %}body{% endcall %}
fn renderCallBlock(node: *const CompiledNode, context: *c.PyObject, out: *OutputBuffer, depth: usize) void {
    const tmpl = current_template orelse return;
    const macro_idx = tmpl.macros.get(node.block_name) orelse return;
    if (macro_idx >= tmpl.nodes.len) return;
    const macro_node = &tmpl.nodes[macro_idx];

    // Render the caller's body first and store as "caller" variable
    var caller_out = OutputBuffer.init();
    defer caller_out.deinit();
    _ = renderNodes(node.children, context, &caller_out, depth + 1, null);

    // Create context with args + caller() result
    const call_ctx = c.PyDict_Copy(context) orelse return;
    defer c.Py_DecRef(call_ctx);

    // Bind arguments
    for (macro_node.macro_params, 0..) |*param, pi| {
        const param_key = allocator.dupeZ(u8, param.name) catch continue;
        defer allocator.free(param_key);
        if (pi < node.macro_args.len) {
            const arg_str = node.macro_args[pi];
            var arg_val: ?*c.PyObject = null;
            if (arg_str.len >= 2 and (arg_str[0] == '\'' or arg_str[0] == '"')) {
                arg_val = py.newString(arg_str[1 .. arg_str.len - 1]);
            } else {
                const path = parseVarPath(arg_str) catch continue;
                arg_val = resolveVar(&path, context);
                freeVarPath(@constCast(&path));
            }
            if (arg_val) |v| {
                _ = c.PyDict_SetItemString(call_ctx, param_key.ptr, v);
                c.Py_DecRef(v);
            }
        } else if (param.default_val) |default| {
            const def_val = py.newString(default) orelse continue;
            _ = c.PyDict_SetItemString(call_ctx, param_key.ptr, def_val);
            c.Py_DecRef(def_val);
        }
    }

    // Set caller() result as "caller" variable (for {{ caller() }} inside macro)
    const caller_str = py.newString(caller_out.result()) orelse return;
    _ = c.PyDict_SetItemString(call_ctx, "caller", caller_str);
    c.Py_DecRef(caller_str);

    _ = renderNodes(macro_node.children, call_ctx, out, depth + 1, null);
}

fn applyFilter(filter: *const FilterSpec, value: *c.PyObject, context: *c.PyObject) ?*c.PyObject {
    _ = context;

    if (filter.native_id >= 0) {
        return applyNativeFilter(filter.native_id, value, filter.arg);
    }

    // Python fallback
    if (filter.py_func) |func| {
        const args = c.PyTuple_Pack(1, value) orelse return null;
        defer c.Py_DecRef(args);
        return c.PyObject_CallObject(func, args);
    }

    // Unknown filter — return value unchanged
    c.Py_IncRef(value);
    return value;
}

/// Shared implementation for select/reject filters.
/// `invert=false` → select (keep passing items), `invert=true` → reject (keep failing items)
fn applySelectReject(value: *c.PyObject, arg: ?[]const u8, invert: bool) ?*c.PyObject {
    const test_name = arg orelse "true"; // default: select truthy items
    const test_type = TestType.fromName(test_name);

    if (c.PyList_Check(value) == 0 and c.PyTuple_Check(value) == 0) {
        c.Py_IncRef(value);
        return value;
    }

    const result = c.PyList_New(0) orelse return null;
    const seq_len = c.PyObject_Length(value);
    var i: c.Py_ssize_t = 0;
    while (i < seq_len) : (i += 1) {
        const item = c.PySequence_GetItem(value, i) orelse continue;

        // Evaluate the test against this item
        var passes: bool = switch (test_type) {
            .defined => true, // items in a list are always defined
            .undefined => false,
            .none => item == @as(*c.PyObject, @ptrCast(&c._Py_NoneStruct)),
            .true_ => c.PyObject_IsTrue(item) == 1,
            .false_ => c.PyObject_IsTrue(item) == 0,
            .string => c.PyUnicode_Check(item) != 0,
            .number, .integer => c.PyLong_Check(item) != 0,
            .float_ => c.PyFloat_Check(item) != 0,
            .boolean => c.PyBool_Check(item) != 0,
            .callable => c.PyCallable_Check(item) != 0,
            .mapping => c.PyMapping_Check(item) != 0 and c.PyUnicode_Check(item) == 0,
            .sequence => c.PySequence_Check(item) != 0,
            .odd => c.PyLong_Check(item) != 0 and @mod(c.PyLong_AsLong(item), 2) != 0,
            .even => c.PyLong_Check(item) != 0 and @mod(c.PyLong_AsLong(item), 2) == 0,
            .iterable => blk: {
                const it = c.PyObject_GetIter(item);
                const ok = it != null;
                if (it) |iter| c.Py_DecRef(iter);
                c.PyErr_Clear();
                break :blk ok;
            },
            .upper => blk: {
                const m = c.PyObject_CallMethod(item, "isupper", null);
                const ok = m != null and c.PyObject_IsTrue(m) == 1;
                if (m) |mm| c.Py_DecRef(mm);
                break :blk ok;
            },
            .lower => blk: {
                const m = c.PyObject_CallMethod(item, "islower", null);
                const ok = m != null and c.PyObject_IsTrue(m) == 1;
                if (m) |mm| c.Py_DecRef(mm);
                break :blk ok;
            },
            else => c.PyObject_IsTrue(item) == 1, // fallback: truthiness
        };

        if (invert) passes = !passes;

        if (passes) {
            _ = c.PyList_Append(result, item);
        }
        c.Py_DecRef(item);
    }
    return result;
}

fn applyNativeFilter(filter_id: i32, value: *c.PyObject, arg: ?[]const u8) ?*c.PyObject {
    switch (filter_id) {
        FILTER_ESCAPE => {
            // Mark as needing escape (handled during writeValue)
            c.Py_IncRef(value);
            return value;
        },
        FILTER_SAFE => {
            c.Py_IncRef(value);
            return value;
        },
        FILTER_LOWER => {
            const method = c.PyObject_GetAttrString(value, "lower") orelse {
                c.PyErr_Clear();
                c.Py_IncRef(value);
                return value;
            };
            defer c.Py_DecRef(method);
            return c.PyObject_CallNoArgs(method);
        },
        FILTER_UPPER => {
            const method = c.PyObject_GetAttrString(value, "upper") orelse {
                c.PyErr_Clear();
                c.Py_IncRef(value);
                return value;
            };
            defer c.Py_DecRef(method);
            return c.PyObject_CallNoArgs(method);
        },
        FILTER_TITLE => {
            const method = c.PyObject_GetAttrString(value, "title") orelse {
                c.PyErr_Clear();
                c.Py_IncRef(value);
                return value;
            };
            defer c.Py_DecRef(method);
            return c.PyObject_CallNoArgs(method);
        },
        FILTER_CAPITALIZE => {
            const method = c.PyObject_GetAttrString(value, "capitalize") orelse {
                c.PyErr_Clear();
                c.Py_IncRef(value);
                return value;
            };
            defer c.Py_DecRef(method);
            return c.PyObject_CallNoArgs(method);
        },
        FILTER_TRIM => {
            const method = c.PyObject_GetAttrString(value, "strip") orelse {
                c.PyErr_Clear();
                c.Py_IncRef(value);
                return value;
            };
            defer c.Py_DecRef(method);
            return c.PyObject_CallNoArgs(method);
        },
        FILTER_LENGTH => {
            const len = c.PyObject_Length(value);
            if (len >= 0) return py.newInt(len);
            // Non-iterable (None, int, float, bool) → clear error, return 0
            c.PyErr_Clear();
            return py.newInt(0);
        },
        FILTER_DEFAULT => {
            if (c.PyObject_IsTrue(value) != 1) {
                if (arg) |a| return py.newString(a);
            }
            c.Py_IncRef(value);
            return value;
        },
        FILTER_FIRST => {
            const item = c.PySequence_GetItem(value, 0);
            if (item) |it| return it;
            c.PyErr_Clear();
            return py.pyNone();
        },
        FILTER_LAST => {
            const len = c.PyObject_Length(value);
            if (len > 0) {
                const item = c.PySequence_GetItem(value, len - 1);
                if (item) |it| return it;
                c.PyErr_Clear();
            }
            return py.pyNone();
        },
        FILTER_JOIN => {
            const sep = if (arg) |a| py.newString(a) orelse py.newString("") else py.newString("");
            defer if (sep) |s| c.Py_DecRef(s);
            // Try direct join first (works for list of strings)
            const direct = c.PyUnicode_Join(sep, value);
            if (direct) |d| return d;
            c.PyErr_Clear();
            // Fallback: convert each item to string, then join
            const str_list = c.PyList_New(0) orelse {
                c.Py_IncRef(value);
                return value;
            };
            defer c.Py_DecRef(str_list);
            const iter = c.PyObject_GetIter(value) orelse {
                c.Py_IncRef(value);
                return value;
            };
            defer c.Py_DecRef(iter);
            while (c.PyIter_Next(iter)) |item| {
                const s = c.PyObject_Str(item) orelse item;
                _ = c.PyList_Append(str_list, s);
                if (s != item) c.Py_DecRef(s);
                c.Py_DecRef(item);
            }
            return c.PyUnicode_Join(sep, str_list) orelse {
                c.Py_IncRef(value);
                return value;
            };
        },
        FILTER_INT => {
            const result = c.PyNumber_Long(value);
            if (result) |r| return r;
            c.PyErr_Clear();
            return py.newInt(0);
        },
        FILTER_FLOAT => {
            const result = c.PyNumber_Float(value);
            if (result) |r| return r;
            c.PyErr_Clear();
            return c.PyFloat_FromDouble(0.0);
        },
        FILTER_STRING => {
            return c.PyObject_Str(value);
        },
        FILTER_REPLACE => {
            // replace('old', 'new') — call Python str.replace(old, new)
            // arg contains raw multi-arg string like "'old', 'new'" from parseFilter
            const raw = arg orelse {
                c.Py_IncRef(value);
                return value;
            };
            // Parse two quoted args from the raw string
            // Format: 'old', 'new' or "old", "new"
            if (raw.len < 3) {
                c.Py_IncRef(value);
                return value;
            }
            const q1 = raw[0];
            if (q1 != '\'' and q1 != '"') {
                c.Py_IncRef(value);
                return value;
            }
            const end1 = std.mem.indexOfPos(u8, raw, 1, &.{q1}) orelse {
                c.Py_IncRef(value);
                return value;
            };
            const old_str = raw[1..end1];
            // Find second quoted arg after comma
            const comma_pos = std.mem.indexOfPos(u8, raw, end1 + 1, ",") orelse {
                c.Py_IncRef(value);
                return value;
            };
            const rest = std.mem.trim(u8, raw[comma_pos + 1 ..], " ");
            if (rest.len < 2) {
                c.Py_IncRef(value);
                return value;
            }
            const q2 = rest[0];
            const end2 = std.mem.indexOfPos(u8, rest, 1, &.{q2}) orelse rest.len - 1;
            const new_str = rest[1..end2];
            // Call Python str.replace(old, new)
            const py_old = py.newString(old_str) orelse {
                c.Py_IncRef(value);
                return value;
            };
            defer c.Py_DecRef(py_old);
            const py_new = py.newString(new_str) orelse {
                c.Py_IncRef(value);
                return value;
            };
            defer c.Py_DecRef(py_new);
            const py_args = c.PyTuple_Pack(2, py_old, py_new) orelse {
                c.Py_IncRef(value);
                return value;
            };
            defer c.Py_DecRef(py_args);
            const method = c.PyObject_GetAttrString(value, "replace") orelse {
                c.Py_IncRef(value);
                return value;
            };
            defer c.Py_DecRef(method);
            const result = c.PyObject_CallObject(method, py_args);
            if (result == null) {
                c.PyErr_Clear();
                c.Py_IncRef(value);
                return value;
            }
            return result;
        },
        FILTER_WORDCOUNT => {
            // SIMD-accelerated: detect whitespace in 16-byte chunks
            if (c.PyUnicode_Check(value) != 0) {
                var slen: c.Py_ssize_t = 0;
                const sptr = c.PyUnicode_AsUTF8AndSize(value, &slen) orelse return py.newInt(0);
                const data = sptr[0..@intCast(slen)];
                const len = data.len;
                var count: i64 = 0;
                var in_word = false;

                // SIMD fast path: check 16 bytes at a time for whitespace
                const Block16 = @Vector(16, u8);
                const space_vec: Block16 = @splat(' ');
                const tab_vec: Block16 = @splat('\t');
                const nl_vec: Block16 = @splat('\n');
                const cr_vec: Block16 = @splat('\r');
                var i: usize = 0;

                while (i + 16 <= len) {
                    const chunk: Block16 = data[i..][0..16].*;
                    const is_space = (chunk == space_vec) | (chunk == tab_vec) |
                        (chunk == nl_vec) | (chunk == cr_vec);
                    const has_ws = @reduce(.Or, is_space);
                    if (!has_ws) {
                        // No whitespace in chunk — if we weren't in a word, start one
                        if (!in_word) {
                            in_word = true;
                            count += 1;
                        }
                        i += 16;
                    } else {
                        // Has whitespace — fall to scalar for precise word boundaries
                        break;
                    }
                }

                // Scalar remainder
                while (i < len) {
                    const ch = data[i];
                    if (ch == ' ' or ch == '\t' or ch == '\n' or ch == '\r') {
                        in_word = false;
                    } else if (!in_word) {
                        in_word = true;
                        count += 1;
                    }
                    i += 1;
                }
                return py.newInt(count);
            }
            return py.newInt(0);
        },
        FILTER_ABS => {
            const result = c.PyNumber_Absolute(value);
            if (result) |r| return r;
            c.PyErr_Clear();
            c.Py_IncRef(value);
            return value;
        },
        FILTER_ROUND => {
            // round(precision) — default 0
            const precision: c_long = if (arg) |a| std.fmt.parseInt(c_long, a, 10) catch 0 else 0;
            const py_prec = c.PyLong_FromLong(precision);
            defer if (py_prec) |p| c.Py_DecRef(p);
            const result = c.PyObject_CallMethod(value, "__round__", "O", py_prec);
            if (result) |r| return r;
            c.PyErr_Clear();
            c.Py_IncRef(value);
            return value;
        },
        FILTER_SORT => {
            const list_copy = c.PySequence_List(value) orelse {
                c.PyErr_Clear();
                c.Py_IncRef(value);
                return value;
            };
            _ = c.PyList_Sort(list_copy);
            return list_copy;
        },
        FILTER_REVERSE => {
            const list_copy = c.PySequence_List(value) orelse {
                c.PyErr_Clear();
                c.Py_IncRef(value);
                return value;
            };
            _ = c.PyList_Reverse(list_copy);
            return list_copy;
        },
        FILTER_UNIQUE => {
            const result = c.PyList_New(0) orelse {
                // On OOM return the input unchanged — but as a NEW ref, matching
                // every other error path in this filter (the caller owns the result).
                c.Py_IncRef(value);
                return value;
            };
            const seen = c.PySet_New(null) orelse {
                c.Py_DecRef(result);
                c.Py_IncRef(value);
                return value;
            };
            defer c.Py_DecRef(seen);
            const iter = c.PyObject_GetIter(value) orelse {
                c.Py_DecRef(result);
                c.Py_IncRef(value);
                return value;
            };
            defer c.Py_DecRef(iter);
            while (c.PyIter_Next(iter)) |item| {
                if (c.PySet_Contains(seen, item) == 0) {
                    _ = c.PySet_Add(seen, item);
                    _ = c.PyList_Append(result, item);
                }
                c.Py_DecRef(item);
            }
            return result;
        },
        FILTER_TOJSON => {
            // Serialize to JSON — reuse our json_dumps
            const json_mod = c.PyImport_ImportModule("json") orelse {
                c.Py_IncRef(value);
                return value;
            };
            defer c.Py_DecRef(json_mod);
            const dumps = c.PyObject_GetAttrString(json_mod, "dumps") orelse {
                c.Py_IncRef(value);
                return value;
            };
            defer c.Py_DecRef(dumps);
            const args_tuple = c.PyTuple_Pack(1, value) orelse {
                c.Py_IncRef(value);
                return value;
            };
            defer c.Py_DecRef(args_tuple);
            const dumped = c.PyObject_CallObject(dumps, args_tuple) orelse {
                c.Py_IncRef(value);
                return value;
            };
            defer c.Py_DecRef(dumped);
            // HTML-safe JSON (matches Jinja2's htmlsafe_json_dumps): escape the
            // characters that could break out of a <script> block or an HTML
            // attribute into \uXXXX form. These stay valid JSON (unlike HTML
            // entities, which would corrupt the string value) and prevent
            // `{{ x|tojson }}` inside <script> from being terminated by a
            // "</script>" payload. U+2028/U+2029 are already \u-escaped by
            // json.dumps' default ensure_ascii=True, so only ASCII < > & ' remain.
            var json_len: c.Py_ssize_t = 0;
            const json_ptr = c.PyUnicode_AsUTF8AndSize(dumped, &json_len) orelse {
                c.Py_IncRef(dumped);
                return dumped;
            };
            const json_str = json_ptr[0..@intCast(json_len)];
            var jbuf = std.ArrayListUnmanaged(u8).empty;
            defer jbuf.deinit(allocator);
            for (json_str) |ch| {
                switch (ch) {
                    '<' => jbuf.appendSlice(allocator, "\\u003c") catch {},
                    '>' => jbuf.appendSlice(allocator, "\\u003e") catch {},
                    '&' => jbuf.appendSlice(allocator, "\\u0026") catch {},
                    '\'' => jbuf.appendSlice(allocator, "\\u0027") catch {},
                    else => jbuf.append(allocator, ch) catch {},
                }
            }
            return py.newString(jbuf.items) orelse {
                c.Py_IncRef(dumped);
                return dumped;
            };
        },
        FILTER_LIST => {
            return c.PySequence_List(value) orelse {
                c.PyErr_Clear();
                c.Py_IncRef(value);
                return value;
            };
        },
        FILTER_BOOL => {
            return if (c.PyObject_IsTrue(value) == 1) py.pyTrue() else py.pyFalse();
        },
        FILTER_SUM => {
            const builtin = c.PyEval_GetBuiltins() orelse {
                c.Py_IncRef(value);
                return value;
            };
            const sum_fn = c.PyDict_GetItemString(builtin, "sum") orelse {
                c.Py_IncRef(value);
                return value;
            };
            const args_tuple = c.PyTuple_Pack(1, value) orelse {
                c.Py_IncRef(value);
                return value;
            };
            defer c.Py_DecRef(args_tuple);
            return c.PyObject_CallObject(sum_fn, args_tuple) orelse {
                c.PyErr_Clear();
                return py.newInt(0);
            };
        },
        FILTER_MIN => {
            const builtin = c.PyEval_GetBuiltins() orelse {
                c.Py_IncRef(value);
                return value;
            };
            const fn_obj = c.PyDict_GetItemString(builtin, "min") orelse {
                c.Py_IncRef(value);
                return value;
            };
            const at = c.PyTuple_Pack(1, value) orelse {
                c.Py_IncRef(value);
                return value;
            };
            defer c.Py_DecRef(at);
            return c.PyObject_CallObject(fn_obj, at) orelse {
                c.PyErr_Clear();
                c.Py_IncRef(value);
                return value;
            };
        },
        FILTER_MAX => {
            const builtin = c.PyEval_GetBuiltins() orelse {
                c.Py_IncRef(value);
                return value;
            };
            const fn_obj = c.PyDict_GetItemString(builtin, "max") orelse {
                c.Py_IncRef(value);
                return value;
            };
            const at = c.PyTuple_Pack(1, value) orelse {
                c.Py_IncRef(value);
                return value;
            };
            defer c.Py_DecRef(at);
            return c.PyObject_CallObject(fn_obj, at) orelse {
                c.PyErr_Clear();
                c.Py_IncRef(value);
                return value;
            };
        },
        FILTER_DICTSORT => {
            // sorted(dict.items())
            const items_method = c.PyObject_GetAttrString(value, "items") orelse {
                c.PyErr_Clear();
                c.Py_IncRef(value);
                return value;
            };
            defer c.Py_DecRef(items_method);
            const items = c.PyObject_CallNoArgs(items_method) orelse {
                c.Py_IncRef(value);
                return value;
            };
            defer c.Py_DecRef(items);
            const sorted_list = c.PySequence_List(items) orelse {
                c.Py_IncRef(value);
                return value;
            };
            _ = c.PyList_Sort(sorted_list);
            return sorted_list;
        },
        FILTER_ITEMS => {
            const items_method = c.PyObject_GetAttrString(value, "items") orelse {
                c.PyErr_Clear();
                c.Py_IncRef(value);
                return value;
            };
            defer c.Py_DecRef(items_method);
            const result = c.PyObject_CallNoArgs(items_method) orelse {
                c.Py_IncRef(value);
                return value;
            };
            // Convert to list for iteration
            const list_result = c.PySequence_List(result) orelse {
                return result;
            };
            c.Py_DecRef(result);
            return list_result;
        },
        FILTER_COUNT => {
            // Alias for length
            const len = c.PyObject_Length(value);
            return py.newInt(if (len >= 0) len else 0);
        },
        FILTER_INDENT => {
            // indent(width) — indent each line by width spaces (default 4)
            const width: usize = if (arg) |a| (std.fmt.parseInt(usize, a, 10) catch 4) else 4;
            var str_len: c.Py_ssize_t = 0;
            const str_ptr = c.PyUnicode_AsUTF8AndSize(value, &str_len) orelse return value;
            const data = str_ptr[0..@intCast(str_len)];

            var result: std.ArrayListUnmanaged(u8) = .empty;
            defer result.deinit(allocator);
            var line_start = true;
            for (data) |ch| {
                if (line_start and ch != '\n') {
                    for (0..width) |_| result.append(allocator, ' ') catch {};
                    line_start = false;
                }
                result.append(allocator, ch) catch {};
                if (ch == '\n') line_start = true;
            }
            return py.newString(result.items);
        },
        FILTER_CENTER => {
            // center(width) — center text in field of given width
            const width: usize = if (arg) |a| (std.fmt.parseInt(usize, a, 10) catch 80) else 80;
            var str_len: c.Py_ssize_t = 0;
            const str_ptr = c.PyUnicode_AsUTF8AndSize(value, &str_len) orelse return value;
            const text_len: usize = @intCast(str_len);
            if (text_len >= width) {
                c.Py_IncRef(value);
                return value;
            }
            const total_pad = width - text_len;
            const left_pad = total_pad / 2;
            const right_pad = total_pad - left_pad;
            var buf: std.ArrayListUnmanaged(u8) = .empty;
            defer buf.deinit(allocator);
            for (0..left_pad) |_| buf.append(allocator, ' ') catch {};
            buf.appendSlice(allocator, str_ptr[0..text_len]) catch {};
            for (0..right_pad) |_| buf.append(allocator, ' ') catch {};
            return py.newString(buf.items);
        },
        FILTER_WORDWRAP => {
            // wordwrap(width) — wrap text at word boundaries
            const width: usize = if (arg) |a| (std.fmt.parseInt(usize, a, 10) catch 79) else 79;
            var str_len: c.Py_ssize_t = 0;
            const str_ptr = c.PyUnicode_AsUTF8AndSize(value, &str_len) orelse return value;
            const data = str_ptr[0..@intCast(str_len)];
            var buf: std.ArrayListUnmanaged(u8) = .empty;
            defer buf.deinit(allocator);
            var col: usize = 0;
            for (data) |ch| {
                if (ch == '\n') {
                    buf.append(allocator, '\n') catch {};
                    col = 0;
                    continue;
                }
                if (col >= width and ch == ' ') {
                    buf.append(allocator, '\n') catch {};
                    col = 0;
                    continue;
                }
                buf.append(allocator, ch) catch {};
                col += 1;
            }
            return py.newString(buf.items);
        },
        FILTER_FILESIZEFORMAT => {
            // filesizeformat — format bytes as human-readable
            const val = if (c.PyLong_Check(value) != 0)
                @as(f64, @floatFromInt(c.PyLong_AsLong(value)))
            else if (c.PyFloat_Check(value) != 0)
                c.PyFloat_AsDouble(value)
            else
                0.0;
            var buf: [64]u8 = undefined;
            const s = if (val < 1024.0)
                std.fmt.bufPrint(&buf, "{d:.0} Bytes", .{val}) catch return value
            else if (val < 1024.0 * 1024.0)
                std.fmt.bufPrint(&buf, "{d:.1} kB", .{val / 1024.0}) catch return value
            else if (val < 1024.0 * 1024.0 * 1024.0)
                std.fmt.bufPrint(&buf, "{d:.1} MB", .{val / (1024.0 * 1024.0)}) catch return value
            else
                std.fmt.bufPrint(&buf, "{d:.1} GB", .{val / (1024.0 * 1024.0 * 1024.0)}) catch return value;
            return py.newString(s);
        },
        FILTER_STRIPTAGS => {
            // SIMD-accelerated: scan for '<' in 16-byte chunks, skip tag content
            if (c.PyUnicode_Check(value) == 0) {
                c.Py_IncRef(value);
                return value;
            }
            var slen: c.Py_ssize_t = 0;
            const sptr = c.PyUnicode_AsUTF8AndSize(value, &slen) orelse return value;
            const data = sptr[0..@intCast(slen)];
            const len = data.len;

            var buf: std.ArrayListUnmanaged(u8) = .empty;
            defer buf.deinit(allocator);
            buf.ensureTotalCapacity(allocator, len) catch {};

            var i: usize = 0;
            var in_tag = false;

            // SIMD fast path: scan 16 bytes at a time for '<' and '>'
            const Block16 = @Vector(16, u8);
            const lt_vec: Block16 = @splat('<');
            const gt_vec: Block16 = @splat('>');

            while (!in_tag and i + 16 <= len) {
                const chunk: Block16 = data[i..][0..16].*;
                const has_lt = @reduce(.Or, chunk == lt_vec);
                const has_gt = @reduce(.Or, chunk == gt_vec);

                if (!has_lt and !has_gt) {
                    // No tag boundaries in this chunk — copy entirely
                    buf.appendSlice(allocator, data[i..][0..16]) catch {};
                    i += 16;
                } else {
                    // Has tag boundary — fall to scalar
                    break;
                }
            }

            // Scalar remainder (handles tag boundaries precisely)
            while (i < len) {
                const ch = data[i];
                if (ch == '<') {
                    in_tag = true;
                } else if (ch == '>') {
                    in_tag = false;
                } else if (!in_tag) {
                    buf.append(allocator, ch) catch {};
                }
                i += 1;
            }

            return py.newString(buf.items);
        },
        FILTER_TRUNCATE => {
            // truncate(length) — Jinja2 semantics: total output ≤ length INCLUDING "..." suffix
            const max_len: usize = if (arg) |a| (std.fmt.parseInt(usize, a, 10) catch 255) else 255;
            if (c.PyUnicode_Check(value) == 0) {
                c.Py_IncRef(value);
                return value;
            }
            var slen: c.Py_ssize_t = 0;
            const sptr = c.PyUnicode_AsUTF8AndSize(value, &slen) orelse return value;
            const data = sptr[0..@intCast(slen)];

            if (data.len <= max_len) {
                c.Py_IncRef(value);
                return value;
            }

            const end_marker = "...";
            // Text portion: max_len minus suffix length
            const text_max = if (max_len > end_marker.len) max_len - end_marker.len else 0;

            // Find last space at or before text_max to break at word boundary
            var cut = @min(text_max, data.len);
            // If we're not at a word boundary, search backward for a space
            if (cut < data.len and data[cut] != ' ') {
                var search = cut;
                while (search > 0 and data[search - 1] != ' ') {
                    search -= 1;
                }
                if (search > 0) cut = search; // Found a space — break there
                // else: no space found, hard cut at text_max
            }
            // Trim trailing space at cut point
            while (cut > 0 and data[cut - 1] == ' ') {
                cut -= 1;
            }

            var buf: [4096]u8 = undefined;
            const total = @min(cut + end_marker.len, buf.len);
            @memcpy(buf[0..cut], data[0..cut]);
            @memcpy(buf[cut..][0..end_marker.len], end_marker);
            return py.newString(buf[0..total]);
        },
        FILTER_URLENCODE => {
            // SIMD-accelerated URL encoding
            if (c.PyUnicode_Check(value) == 0) {
                c.Py_IncRef(value);
                return value;
            }
            var slen: c.Py_ssize_t = 0;
            const sptr = c.PyUnicode_AsUTF8AndSize(value, &slen) orelse return value;
            const data = sptr[0..@intCast(slen)];

            // Use native SIMD url_encode from string_ops if available
            var buf: std.ArrayListUnmanaged(u8) = .empty;
            defer buf.deinit(allocator);
            buf.ensureTotalCapacity(allocator, data.len * 3) catch {};

            const hex = "0123456789ABCDEF";
            for (data) |ch| {
                if ((ch >= 'A' and ch <= 'Z') or (ch >= 'a' and ch <= 'z') or
                    (ch >= '0' and ch <= '9') or ch == '-' or ch == '_' or ch == '.' or ch == '~')
                {
                    buf.append(allocator, ch) catch {};
                } else {
                    buf.append(allocator, '%') catch {};
                    buf.append(allocator, hex[ch >> 4]) catch {};
                    buf.append(allocator, hex[ch & 0x0F]) catch {};
                }
            }
            return py.newString(buf.items);
        },
        FILTER_XMLATTR => {
            // {{ {'class': 'btn', 'id': 'submit'}|xmlattr }} → class="btn" id="submit"
            if (c.PyDict_Check(value) == 0) {
                c.Py_IncRef(value);
                return value;
            }
            var buf = std.ArrayListUnmanaged(u8).empty;
            var pos: c.Py_ssize_t = 0;
            var key: ?*c.PyObject = null;
            var val: ?*c.PyObject = null;
            var first = true;
            while (c.PyDict_Next(value, &pos, &key, &val) != 0) {
                if (key == null or val == null) continue;
                // Skip None values
                if (val.? == @as(*c.PyObject, @ptrCast(&c._Py_NoneStruct))) continue;
                // Get key string
                const key_str = if (c.PyUnicode_Check(key.?) != 0) c.PyUnicode_AsUTF8(key.?) else null;
                if (key_str == null) continue;
                const k = std.mem.span(key_str.?);
                // Reject unsafe attribute names (mirrors Jinja2's xmlattr): a key
                // containing whitespace, '/', '>', '=', '"' or '\'' would inject a
                // whole new attribute or event handler (e.g. `x onclick=alert(1)`),
                // since the key is written verbatim (only values are escaped). Skip it.
                if (k.len == 0) continue;
                var unsafe_key = false;
                for (k) |kc| {
                    switch (kc) {
                        ' ', '\t', '\n', '\r', 0x0C, '/', '>', '=', '"', '\'' => {
                            unsafe_key = true;
                        },
                        else => {},
                    }
                    if (unsafe_key) break;
                }
                if (unsafe_key) continue;
                // Get value string
                const val_str_obj = if (c.PyUnicode_Check(val.?) != 0) val else c.PyObject_Str(val.?);
                if (val_str_obj == null) continue;
                const needs_val_decref = val_str_obj != val;
                const v_ptr = c.PyUnicode_AsUTF8(val_str_obj.?);
                if (v_ptr == null) {
                    if (needs_val_decref) c.Py_DecRef(val_str_obj.?);
                    continue;
                }
                const v = std.mem.span(v_ptr.?);
                if (!first) buf.append(allocator, ' ') catch {};
                buf.appendSlice(allocator, k) catch {};
                buf.appendSlice(allocator, "=\"") catch {};
                // HTML-escape the value
                for (v) |ch| {
                    switch (ch) {
                        '&' => buf.appendSlice(allocator, "&amp;") catch {},
                        '<' => buf.appendSlice(allocator, "&lt;") catch {},
                        '>' => buf.appendSlice(allocator, "&gt;") catch {},
                        '"' => buf.appendSlice(allocator, "&quot;") catch {},
                        '\'' => buf.appendSlice(allocator, "&#x27;") catch {},
                        else => buf.append(allocator, ch) catch {},
                    }
                }
                buf.append(allocator, '"') catch {};
                if (needs_val_decref) c.Py_DecRef(val_str_obj.?);
                first = false;
            }
            const result = py.newString(buf.items);
            buf.deinit(allocator);
            return result;
        },
        FILTER_GROUPBY => {
            // {{ users|groupby('role') }} → list of (grouper, list) Namespace objects
            const attr_name = arg orelse {
                c.Py_IncRef(value);
                return value;
            };
            if (c.PyList_Check(value) == 0 and c.PyTuple_Check(value) == 0) {
                c.Py_IncRef(value);
                return value;
            }
            // Build groups: ordered dict[key_str] → list of items
            // Use Python OrderedDict for stable key order
            const collections = c.PyImport_ImportModule("collections") orelse {
                c.PyErr_Clear();
                c.Py_IncRef(value);
                return value;
            };
            defer c.Py_DecRef(collections);
            const od_class = c.PyObject_GetAttrString(collections, "OrderedDict") orelse {
                c.PyErr_Clear();
                c.Py_IncRef(value);
                return value;
            };
            defer c.Py_DecRef(od_class);
            const groups = c.PyObject_CallNoArgs(od_class) orelse {
                c.PyErr_Clear();
                c.Py_IncRef(value);
                return value;
            };
            defer c.Py_DecRef(groups);

            const attr_z = allocator.dupeZ(u8, attr_name) catch {
                c.Py_IncRef(value);
                return value;
            };
            defer allocator.free(attr_z);

            // groupby('attr') resolves `attr` on each item via getattr — in sandbox
            // mode block dangerous names (e.g. `|groupby('__globals__')`) so this
            // filter can't be used to escape the untrusted-template sandbox.
            if (isSandboxBlocked(attr_name)) {
                c.Py_IncRef(value);
                return value;
            }

            const seq_len = c.PyObject_Length(value);
            var si: c.Py_ssize_t = 0;
            while (si < seq_len) : (si += 1) {
                const item = c.PySequence_GetItem(value, si) orelse continue;
                // Get attribute value (try dict key first, then attr)
                var key_val: ?*c.PyObject = null;
                if (c.PyDict_Check(item) != 0) {
                    key_val = c.PyDict_GetItemString(item, attr_z.ptr);
                    if (key_val) |kv| c.Py_IncRef(kv);
                }
                if (key_val == null) {
                    key_val = c.PyObject_GetAttrString(item, attr_z.ptr);
                    if (key_val == null) c.PyErr_Clear();
                }
                const kv = key_val orelse py.pyNone();
                const kv_owned = key_val != null;
                defer if (kv_owned) c.Py_DecRef(kv);

                // Get or create list for this key
                var group_list = c.PyObject_GetItem(groups, kv);
                if (group_list == null) {
                    c.PyErr_Clear();
                    group_list = c.PyList_New(0);
                    if (group_list) |gl| {
                        _ = c.PyObject_SetItem(groups, kv, gl);
                    }
                }
                if (group_list) |gl| {
                    _ = c.PyList_Append(gl, item);
                    c.Py_DecRef(gl);
                }
                c.Py_DecRef(item);
            }

            // Convert to list of Namespace-like objects with .grouper and .list
            const result_list = c.PyList_New(0) orelse {
                c.Py_IncRef(value);
                return value;
            };
            const items_method = c.PyObject_CallMethod(groups, "items", null) orelse {
                c.Py_DecRef(result_list);
                c.Py_IncRef(value);
                return value;
            };
            defer c.Py_DecRef(items_method);
            const items_iter = c.PyObject_GetIter(items_method) orelse {
                c.Py_DecRef(result_list);
                c.Py_IncRef(value);
                return value;
            };
            defer c.Py_DecRef(items_iter);
            while (c.PyIter_Next(items_iter)) |pair| {
                defer c.Py_DecRef(pair);
                const grouper = c.PyTuple_GetItem(pair, 0) orelse continue;
                const group_items = c.PyTuple_GetItem(pair, 1) orelse continue;
                // Create a dict with grouper and list keys (Jinja2 returns namedtuple-like)
                const group_obj = c.PyDict_New() orelse continue;
                _ = c.PyDict_SetItemString(group_obj, "grouper", grouper);
                _ = c.PyDict_SetItemString(group_obj, "list", group_items);
                _ = c.PyList_Append(result_list, group_obj);
                c.Py_DecRef(group_obj);
            }
            return result_list;
        },
        FILTER_SELECT => {
            // {{ items|select('defined') }} → keep items that pass the test
            return applySelectReject(value, arg, false);
        },
        FILTER_REJECT => {
            // {{ items|reject('none') }} → keep items that FAIL the test
            return applySelectReject(value, arg, true);
        },
        FILTER_URLIZE => {
            // Convert URLs and email addresses in text to clickable HTML links
            if (c.PyUnicode_Check(value) == 0) {
                c.Py_IncRef(value);
                return value;
            }
            var str_len: c.Py_ssize_t = 0;
            const str_ptr = c.PyUnicode_AsUTF8AndSize(value, &str_len) orelse {
                c.Py_IncRef(value);
                return value;
            };
            const text = str_ptr[0..@intCast(str_len)];
            var buf = std.ArrayListUnmanaged(u8).empty;

            var pos: usize = 0;
            while (pos < text.len) {
                // Scan for URL patterns
                if (pos + 7 <= text.len and (std.mem.eql(u8, text[pos..][0..7], "http://") or
                    (pos + 8 <= text.len and std.mem.eql(u8, text[pos..][0..8], "https://"))))
                {
                    // Found http:// or https:// — extract URL until whitespace or certain punctuation
                    const url_start = pos;
                    var url_end = pos;
                    while (url_end < text.len and text[url_end] != ' ' and text[url_end] != '\t' and
                        text[url_end] != '\n' and text[url_end] != '\r' and
                        text[url_end] != '<' and text[url_end] != '>')
                    {
                        url_end += 1;
                    }
                    // Strip trailing punctuation that's likely not part of URL
                    while (url_end > url_start and (text[url_end - 1] == '.' or text[url_end - 1] == ',' or
                        text[url_end - 1] == ')' or text[url_end - 1] == '!' or text[url_end - 1] == '?'))
                    {
                        url_end -= 1;
                    }
                    const url = text[url_start..url_end];
                    buf.appendSlice(allocator, "<a href=\"") catch {};
                    // HTML-escape the URL for the href attribute
                    for (url) |ch| {
                        switch (ch) {
                            '&' => buf.appendSlice(allocator, "&amp;") catch {},
                            '"' => buf.appendSlice(allocator, "&quot;") catch {},
                            else => buf.append(allocator, ch) catch {},
                        }
                    }
                    buf.appendSlice(allocator, "\" rel=\"noopener\">") catch {};
                    // HTML-escape the display text
                    for (url) |ch| {
                        switch (ch) {
                            '&' => buf.appendSlice(allocator, "&amp;") catch {},
                            '<' => buf.appendSlice(allocator, "&lt;") catch {},
                            '>' => buf.appendSlice(allocator, "&gt;") catch {},
                            else => buf.append(allocator, ch) catch {},
                        }
                    }
                    buf.appendSlice(allocator, "</a>") catch {};
                    pos = url_end;
                } else if (pos + 4 <= text.len and std.mem.eql(u8, text[pos..][0..4], "www.")) {
                    // www. prefix — extract URL, link with http:// prefix
                    const url_start = pos;
                    var url_end = pos;
                    while (url_end < text.len and text[url_end] != ' ' and text[url_end] != '\t' and
                        text[url_end] != '\n' and text[url_end] != '\r' and
                        text[url_end] != '<' and text[url_end] != '>')
                    {
                        url_end += 1;
                    }
                    while (url_end > url_start and (text[url_end - 1] == '.' or text[url_end - 1] == ',' or
                        text[url_end - 1] == ')' or text[url_end - 1] == '!' or text[url_end - 1] == '?'))
                    {
                        url_end -= 1;
                    }
                    const domain = text[url_start..url_end];
                    // HTML-escape the domain before embedding it in the href attribute
                    // AND the link text — otherwise a payload like
                    // `www.a"onmouseover="alert(1)` breaks out of the attribute (the
                    // domain scan stops at space/</> but NOT at `"`).
                    buf.appendSlice(allocator, "<a href=\"http://") catch {};
                    htmlEscapeAppend(&buf, domain);
                    buf.appendSlice(allocator, "\" rel=\"noopener\">") catch {};
                    htmlEscapeAppend(&buf, domain);
                    buf.appendSlice(allocator, "</a>") catch {};
                    pos = url_end;
                } else if (text[pos] != ' ' and text[pos] != '\t' and text[pos] != '\n') {
                    // Check for email: scan word, if contains @ with text on both sides
                    const word_start = pos;
                    var word_end = pos;
                    while (word_end < text.len and text[word_end] != ' ' and text[word_end] != '\t' and
                        text[word_end] != '\n' and text[word_end] != '\r' and
                        text[word_end] != '<' and text[word_end] != '>')
                    {
                        word_end += 1;
                    }
                    while (word_end > word_start and (text[word_end - 1] == '.' or text[word_end - 1] == ',' or
                        text[word_end - 1] == ')' or text[word_end - 1] == '!' or text[word_end - 1] == '?'))
                    {
                        word_end -= 1;
                    }
                    const word = text[word_start..word_end];
                    if (std.mem.indexOf(u8, word, "@")) |at_pos| {
                        if (at_pos > 0 and at_pos < word.len - 1 and std.mem.indexOf(u8, word[at_pos + 1 ..], ".") != null) {
                            // Looks like an email address — HTML-escape the address in
                            // both the mailto: href and the link text (same attribute-
                            // breakout risk as the www. branch: the word scan does not
                            // stop at `"`).
                            buf.appendSlice(allocator, "<a href=\"mailto:") catch {};
                            htmlEscapeAppend(&buf, word);
                            buf.appendSlice(allocator, "\">") catch {};
                            htmlEscapeAppend(&buf, word);
                            buf.appendSlice(allocator, "</a>") catch {};
                            pos = word_end;
                            continue;
                        }
                    }
                    // Not a URL or email — HTML-escape and output character by character
                    switch (text[pos]) {
                        '&' => buf.appendSlice(allocator, "&amp;") catch {},
                        '<' => buf.appendSlice(allocator, "&lt;") catch {},
                        '>' => buf.appendSlice(allocator, "&gt;") catch {},
                        '"' => buf.appendSlice(allocator, "&quot;") catch {},
                        else => buf.append(allocator, text[pos]) catch {},
                    }
                    pos += 1;
                } else {
                    buf.append(allocator, text[pos]) catch {};
                    pos += 1;
                }
            }

            const result = py.newString(buf.items);
            buf.deinit(allocator);
            return result;
        },
        FILTER_BATCH, FILTER_MAP, FILTER_FORMAT, FILTER_ATTR => {
            // Complex filters — pass through (Python fallback handles them)
            c.Py_IncRef(value);
            return value;
        },
        else => {
            c.Py_IncRef(value);
            return value;
        },
    }
}

// ── Python C API entry points ────────────────────────────────────────────────

/// _template_compile(source_str, path_str) → PyCapsule(CompiledTemplate*)
/// _template_set_undefined_mode(mode_int) — set undefined variable behavior
/// 0 = silent (default), 1 = strict (raise error), 2 = debug (show name)
/// Module cleanup — release all threadlocal Python object references.
/// Called from m_free at interpreter shutdown to prevent dangling pointer access.
pub fn module_cleanup() void {
    if (template_loader) |loader| {
        c.Py_DecRef(loader);
        template_loader = null;
    }
}

/// _template_set_safety_limits(max_string_len, max_array_count, max_expr_depth)
/// Pass 0 for any value to reset to default.
pub fn py_template_set_safety_limits(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var max_str: c_int = 0;
    var max_arr: c_int = 0;
    var max_dep: c_int = 0;
    if (c.PyArg_ParseTuple(args, "iii", &max_str, &max_arr, &max_dep) == 0) return null;
    safety_limits = .{
        .max_string_len = if (max_str > 0) @intCast(max_str) else 10_000_000,
        .max_array_count = if (max_arr > 0) @intCast(max_arr) else 100_000,
        .max_expr_depth = if (max_dep > 0) @intCast(max_dep) else 500,
    };
    return py.pyNone();
}

/// _template_set_sandbox(enabled) — enable/disable sandbox mode
pub fn py_template_set_sandbox(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var enabled: c_int = 0;
    if (c.PyArg_ParseTuple(args, "i", &enabled) == 0) return null;
    sandbox_enabled = enabled != 0;
    return py.pyNone();
}

/// _template_set_autoescape(enabled) — set the engine-level autoescape default.
///
/// Sets the base autoescape state for the CURRENT THREAD (like the other
/// per-thread render-config setters). Inline {% autoescape true/false %}
/// blocks still override this within their scope and restore this base value
/// afterward, so a `TemplateEngine(autoescape=False)` default is honored while
/// per-block overrides keep working. Pass 1 to escape by default, 0 to disable.
pub fn py_template_set_autoescape(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var enabled: c_int = 0;
    if (c.PyArg_ParseTuple(args, "i", &enabled) == 0) return null;
    autoescape_enabled = enabled != 0;
    return py.pyNone();
}

/// _template_set_i18n_callback(callback_or_none)
/// Set the translation function for {% trans %} blocks.
/// Pass None to disable translation (renders raw key).
pub fn py_template_set_i18n_callback(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var callback: ?*c.PyObject = null;
    if (c.PyArg_ParseTuple(args, "O", &callback) == 0) return null;
    // Release previous callback
    if (i18n_callback) |prev| c.Py_DecRef(prev);
    // Set new callback (or clear if None)
    if (callback) |cb| {
        if (py.isNone(cb)) {
            i18n_callback = null;
        } else {
            c.Py_IncRef(cb);
            i18n_callback = cb;
        }
    } else {
        i18n_callback = null;
    }
    return py.pyNone();
}

/// _template_set_delimiters(block_start, block_end, var_start, var_end, comment_start, comment_end)
/// Pass empty string to reset to defaults.
pub fn py_template_set_delimiters(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var bs_ptr: [*c]const u8 = null;
    var bs_len: c.Py_ssize_t = 0;
    var be_ptr: [*c]const u8 = null;
    var be_len: c.Py_ssize_t = 0;
    var vs_ptr: [*c]const u8 = null;
    var vs_len: c.Py_ssize_t = 0;
    var ve_ptr: [*c]const u8 = null;
    var ve_len: c.Py_ssize_t = 0;
    var cs_ptr: [*c]const u8 = null;
    var cs_len: c.Py_ssize_t = 0;
    var ce_ptr: [*c]const u8 = null;
    var ce_len: c.Py_ssize_t = 0;
    if (c.PyArg_ParseTuple(args, "s#s#s#s#s#s#", &bs_ptr, &bs_len, &be_ptr, &be_len, &vs_ptr, &vs_len, &ve_ptr, &ve_len, &cs_ptr, &cs_len, &ce_ptr, &ce_len) == 0) return null;

    const bs = bs_ptr[0..@intCast(bs_len)];
    if (bs.len == 0) {
        custom_delimiters = null; // reset to defaults
    } else {
        custom_delimiters = .{
            .block_start = allocator.dupe(u8, bs) catch return py.pyNone(),
            .block_end = allocator.dupe(u8, be_ptr[0..@intCast(be_len)]) catch return py.pyNone(),
            .var_start = allocator.dupe(u8, vs_ptr[0..@intCast(vs_len)]) catch return py.pyNone(),
            .var_end = allocator.dupe(u8, ve_ptr[0..@intCast(ve_len)]) catch return py.pyNone(),
            .comment_start = allocator.dupe(u8, cs_ptr[0..@intCast(cs_len)]) catch return py.pyNone(),
            .comment_end = allocator.dupe(u8, ce_ptr[0..@intCast(ce_len)]) catch return py.pyNone(),
        };
    }
    return py.pyNone();
}

pub fn py_template_set_undefined_mode(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var mode_val: c_int = 0;
    if (c.PyArg_ParseTuple(args, "i", &mode_val) == 0) return null;
    undefined_mode = switch (mode_val) {
        1 => .strict,
        2 => .debug,
        else => .silent,
    };
    return py.pyNone();
}

pub fn py_template_compile(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var source_ptr: [*c]const u8 = null;
    var source_len: c.Py_ssize_t = 0;
    var path_ptr: [*c]const u8 = null;
    var path_len: c.Py_ssize_t = 0;
    if (c.PyArg_ParseTuple(args, "s#s#", &source_ptr, &source_len, &path_ptr, &path_len) == 0) return null;

    const source = source_ptr[0..@intCast(source_len)];
    const path = path_ptr[0..@intCast(path_len)];

    const tmpl = compile(source, path) catch {
        // Only set generic error if Zig didn't already set a specific Python exception
        if (c.PyErr_Occurred() == null) {
            py.setError("Failed to compile template: {s}", .{path});
        }
        return null;
    };

    return c.PyCapsule_New(tmpl, "hyperdjango.compiled_template", templateCapsuleDestructor);
}

fn templateCapsuleDestructor(capsule: ?*c.PyObject) callconv(.c) void {
    // During interpreter finalization, Python objects may already be destroyed.
    // Skip cleanup to avoid Py_DecRef on finalized objects (segfault).
    if (c.Py_IsFinalizing() != 0) return;
    if (capsule) |cap| {
        const ptr = c.PyCapsule_GetPointer(cap, "hyperdjango.compiled_template") orelse return;
        const tmpl: *CompiledTemplate = @ptrCast(@alignCast(ptr));
        tmpl.deinit();
        allocator.destroy(tmpl);
    }
}

/// _template_render(capsule, context_dict) → bytes
pub fn py_template_render(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var capsule: ?*c.PyObject = null;
    var context: ?*c.PyObject = null;
    if (c.PyArg_ParseTuple(args, "OO", &capsule, &context) == 0) return null;

    const ptr = c.PyCapsule_GetPointer(capsule.?, "hyperdjango.compiled_template") orelse {
        py.setError("Invalid template capsule", .{});
        return null;
    };
    const tmpl: *const CompiledTemplate = @ptrCast(@alignCast(ptr));

    return render(tmpl, context.?);
}

/// _template_register_filter(capsule, name, callable) — register Python fallback filter
pub fn py_template_register_filter(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var capsule: ?*c.PyObject = null;
    var name_ptr: [*c]const u8 = null;
    var func: ?*c.PyObject = null;
    if (c.PyArg_ParseTuple(args, "OsO", &capsule, &name_ptr, &func) == 0) return null;

    const ptr = c.PyCapsule_GetPointer(capsule.?, "hyperdjango.compiled_template") orelse {
        py.setError("Invalid template capsule", .{});
        return null;
    };
    var tmpl: *CompiledTemplate = @ptrCast(@alignCast(ptr));

    const name = std.mem.span(name_ptr);
    c.Py_IncRef(func.?);
    tmpl.py_filters.put(allocator, name, func.?) catch {
        c.Py_DecRef(func.?);
        py.setError("Failed to register filter", .{});
        return null;
    };

    // Walk all nodes and wire up any matching filter specs
    wireFilterFunc(tmpl.nodes, name, func.?);

    return py.pyNone();
}

/// _template_set_loader(callable) — set the template loader for extends/import resolution
/// The callable receives a template path string and returns the source string.
pub fn py_template_set_loader(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var loader: ?*c.PyObject = null;
    if (c.PyArg_ParseTuple(args, "O", &loader) == 0) return null;
    if (loader) |l| {
        if (template_loader) |old| c.Py_DecRef(old);
        c.Py_IncRef(l);
        template_loader = l;
    }
    return py.pyNone();
}

fn wireFilterFunc(nodes: []CompiledNode, name: []const u8, func: *c.PyObject) void {
    for (nodes) |*node| {
        switch (node.type) {
            .variable => {
                for (node.filters) |*f| {
                    if (f.native_id < 0 and std.mem.eql(u8, f.name, name)) {
                        f.py_func = func;
                    }
                }
            },
            .if_block => {
                for (node.if_branches) |*branch| {
                    wireFilterFunc(branch.body, name, func);
                }
            },
            .for_block => {
                wireFilterFunc(node.for_body, name, func);
                wireFilterFunc(node.for_empty, name, func);
            },
            .block_def, .include, .dynamic_include, .with_block, .autoescape_block => wireFilterFunc(node.children, name, func),
            else => {},
        }
    }
}

// ── Template bytecode serialization / deserialization ─────────────────────────
// Binary format for persisting compiled template node trees to disk.
// Eliminates re-parsing on cold starts. Uses FNV-1a source hash + format
// version for cache invalidation.

const CACHE_MAGIC = [4]u8{ 'H', 'Z', 'T', 'C' }; // HyperZig Template Cache
const CACHE_FORMAT_VERSION: u16 = 1;
const NULL_SENTINEL: u32 = 0xFFFFFFFF;

// Safety limits for deserialization — configurable via _template_set_safety_limits()
// Defaults are conservative; apps can raise them for trusted content or lower for sandboxed use.
const SafetyLimits = struct {
    max_string_len: u32 = 10_000_000, // 10MB max string
    max_array_count: u32 = 100_000, // 100K max nodes/filters/etc
    max_expr_depth: u32 = 500, // max recursive Expr nesting
};
threadlocal var safety_limits: SafetyLimits = .{};

// ── ByteWriter: append-only buffer for serialization ──

const ByteWriter = struct {
    buf: std.ArrayListUnmanaged(u8) = .empty,

    fn writeU8(self: *ByteWriter, v: u8) void {
        self.buf.append(allocator, v) catch {};
    }
    fn writeU16(self: *ByteWriter, v: u16) void {
        self.buf.appendSlice(allocator, &std.mem.toBytes(v)) catch {};
    }
    fn writeU32(self: *ByteWriter, v: u32) void {
        self.buf.appendSlice(allocator, &std.mem.toBytes(v)) catch {};
    }
    fn writeI64(self: *ByteWriter, v: i64) void {
        self.buf.appendSlice(allocator, &std.mem.toBytes(v)) catch {};
    }
    fn writeF64(self: *ByteWriter, v: f64) void {
        self.buf.appendSlice(allocator, &std.mem.toBytes(v)) catch {};
    }
    fn writeString(self: *ByteWriter, s: []const u8) void {
        self.writeU32(@intCast(s.len));
        self.buf.appendSlice(allocator, s) catch {};
    }
    fn writeOptString(self: *ByteWriter, s: ?[]const u8) void {
        if (s) |v| {
            self.writeString(v);
        } else {
            self.writeU32(NULL_SENTINEL);
        }
    }
    fn writeBool(self: *ByteWriter, v: bool) void {
        self.writeU8(if (v) 1 else 0);
    }

    fn writeVarPath(self: *ByteWriter, vp: *const VarPath) void {
        self.writeU16(@intCast(vp.parts.len));
        for (vp.parts) |part| self.writeString(part);
    }
    fn writeFilterSpec(self: *ByteWriter, f: *const FilterSpec) void {
        self.writeString(f.name);
        self.writeOptString(f.arg);
        self.writeU32(@bitCast(f.native_id));
    }
    fn writeExpr(self: *ByteWriter, maybe_expr: ?*const Expr) void {
        if (maybe_expr) |e| {
            self.writeU8(1); // present
            self.writeU8(@intFromEnum(e.type));
            self.writeU8(@intFromEnum(e.cmp_op));
            self.writeBool(e.negate);
            self.writeI64(e.int_val);
            self.writeF64(e.float_val);
            // For literal_var, str_val is empty (aliased into var_path)
            if (e.type == .literal_var) {
                self.writeString("");
            } else {
                self.writeString(e.str_val);
            }
            self.writeVarPath(&e.var_path);
            self.writeExpr(e.left);
            self.writeExpr(e.right);
            self.writeExpr(e.ternary_false);
            if (e.call_args) |args| {
                self.writeU16(@intCast(args.len));
                for (args) |arg| self.writeExpr(arg);
            } else {
                self.writeU16(0xFFFF); // null sentinel
            }
        } else {
            self.writeU8(0); // null
        }
    }
    fn writeNode(self: *ByteWriter, node: *const CompiledNode) void {
        self.writeU8(@intFromEnum(node.type));
        self.writeBool(node.ignore_missing);
        self.writeString(node.text);
        self.writeString(node.block_name);
        self.writeString(node.set_name);
        self.writeString(node.for_var);
        self.writeVarPath(&node.var_path);
        self.writeVarPath(&node.for_iter);
        // Filters
        self.writeU16(@intCast(node.filters.len));
        for (node.filters) |*f| self.writeFilterSpec(f);
        self.writeU16(@intCast(node.for_iter_filters.len));
        for (node.for_iter_filters) |*f| self.writeFilterSpec(f);
        // If branches
        self.writeU16(@intCast(node.if_branches.len));
        for (node.if_branches) |*branch| {
            self.writeExpr(branch.condition_expr);
            self.writeU32(@intCast(branch.body.len));
            for (branch.body) |*child| self.writeNode(child);
        }
        // Child node arrays
        self.writeU32(@intCast(node.for_body.len));
        for (node.for_body) |*child| self.writeNode(child);
        self.writeU32(@intCast(node.for_empty.len));
        for (node.for_empty) |*child| self.writeNode(child);
        self.writeU32(@intCast(node.children.len));
        for (node.children) |*child| self.writeNode(child);
        // Super children
        if (node.super_children) |sc| {
            self.writeU8(1);
            self.writeU32(@intCast(sc.len));
            for (sc) |*child| self.writeNode(child);
        } else {
            self.writeU8(0);
        }
        // Macro params
        self.writeU16(@intCast(node.macro_params.len));
        for (node.macro_params) |*p| {
            self.writeString(p.name);
            self.writeOptString(p.default_val);
        }
        // Macro args
        self.writeU16(@intCast(node.macro_args.len));
        for (node.macro_args) |a| self.writeString(a);
        // Expression
        self.writeExpr(node.expr);
    }

    fn deinit(self: *ByteWriter) void {
        self.buf.deinit(allocator);
    }
};

// ── ByteReader: position-tracked reader for deserialization ──

const ByteReader = struct {
    data: []const u8,
    pos: usize = 0,
    expr_depth: u32 = 0, // tracks recursive Expr nesting to prevent stack overflow
    limit_exceeded: bool = false, // set when a safety limit rejects data

    fn readU8(self: *ByteReader) ?u8 {
        if (self.pos >= self.data.len) return null;
        const v = self.data[self.pos];
        self.pos += 1;
        return v;
    }
    fn readU16(self: *ByteReader) ?u16 {
        if (self.data.len - self.pos < 2) return null;
        const v = std.mem.readInt(u16, self.data[self.pos..][0..2], .little);
        self.pos += 2;
        return v;
    }
    fn readU32(self: *ByteReader) ?u32 {
        if (self.data.len - self.pos < 4) return null;
        const v = std.mem.readInt(u32, self.data[self.pos..][0..4], .little);
        self.pos += 4;
        return v;
    }
    fn readI64(self: *ByteReader) ?i64 {
        if (self.data.len - self.pos < 8) return null;
        const v = std.mem.readInt(i64, self.data[self.pos..][0..8], .little);
        self.pos += 8;
        return v;
    }
    fn readF64(self: *ByteReader) ?f64 {
        if (self.data.len - self.pos < 8) return null;
        const v: f64 = @bitCast(std.mem.readInt(u64, self.data[self.pos..][0..8], .little));
        self.pos += 8;
        return v;
    }
    fn readString(self: *ByteReader) ?[]const u8 {
        const len = self.readU32() orelse return null;
        if (len == NULL_SENTINEL) return null;
        if (len > safety_limits.max_string_len) {
            self.limit_exceeded = true;
            return null;
        }
        if (len > self.data.len - self.pos) return null;
        const s = allocator.dupe(u8, self.data[self.pos..][0..len]) catch return null;
        self.pos += len;
        return s;
    }
    fn readOptString(self: *ByteReader) struct { is_null: bool, val: ?[]const u8 } {
        const len = self.readU32() orelse return .{ .is_null = true, .val = null };
        if (len == NULL_SENTINEL) return .{ .is_null = true, .val = null };
        if (len > safety_limits.max_string_len) {
            self.limit_exceeded = true;
            return .{ .is_null = true, .val = null };
        }
        if (len > self.data.len - self.pos) return .{ .is_null = true, .val = null };
        const s = allocator.dupe(u8, self.data[self.pos..][0..len]) catch return .{ .is_null = true, .val = null };
        self.pos += len;
        return .{ .is_null = false, .val = s };
    }
    fn readBool(self: *ByteReader) ?bool {
        const v = self.readU8() orelse return null;
        return v != 0;
    }

    fn readVarPath(self: *ByteReader) ?VarPath {
        const count = self.readU16() orelse return null;
        if (count == 0) return VarPath{ .parts = &.{} };
        const parts = allocator.alloc([:0]const u8, count) catch return null;
        for (parts, 0..) |*part, i| {
            // VarPath parts are null-terminated for zero-alloc render
            // lookups. Read raw bytes + allocate a sentinel-terminated
            // copy (readString returns a plain []const u8).
            const raw = self.readString() orelse {
                // cleanup partial
                for (parts[0..i]) |p| allocator.free(p);
                allocator.free(parts);
                return null;
            };
            defer allocator.free(raw);
            part.* = allocator.dupeZ(u8, raw) catch {
                for (parts[0..i]) |p| allocator.free(p);
                allocator.free(parts);
                return null;
            };
        }
        return VarPath{ .parts = parts };
    }
    fn readFilterSpec(self: *ByteReader) ?FilterSpec {
        const name = self.readString() orelse return null;
        const opt = self.readOptString();
        const native_id_raw = self.readU32() orelse return null;
        return FilterSpec{
            .name = name,
            .arg = opt.val,
            .native_id = @bitCast(native_id_raw),
            .py_func = null, // re-wired at runtime
        };
    }
    fn readExpr(self: *ByteReader) ?*Expr {
        const presence = self.readU8() orelse return null;
        if (presence == 0) return null;

        if (self.expr_depth >= safety_limits.max_expr_depth) {
            self.limit_exceeded = true;
            return null;
        }
        self.expr_depth += 1;
        defer self.expr_depth -= 1;

        const e = allocator.create(Expr) catch return null;
        e.* = makeExpr();

        // Validate enum values before @enumFromInt to prevent undefined behavior
        const type_byte = self.readU8() orelse {
            allocator.destroy(e);
            return null;
        };
        const max_expr_type = @intFromEnum(ExprType.literal_tuple);
        if (type_byte > max_expr_type) {
            allocator.destroy(e);
            return null;
        }
        e.type = @enumFromInt(type_byte);

        const cmp_byte = self.readU8() orelse {
            allocator.destroy(e);
            return null;
        };
        if (cmp_byte > @intFromEnum(CompareOp.ge)) {
            allocator.destroy(e);
            return null;
        }
        e.cmp_op = @enumFromInt(cmp_byte);

        e.negate = self.readBool() orelse false;
        e.int_val = self.readI64() orelse 0;
        e.float_val = self.readF64() orelse 0.0;

        // Handle str_val ownership: literal_var nodes don't own str_val
        const str = self.readString() orelse "";
        if (e.type == .literal_var) {
            // literal_var doesn't own str_val — free any read string and set to empty
            if (str.len > 0) allocator.free(str);
            e.str_val = "";
        } else {
            e.str_val = str;
        }

        e.var_path = self.readVarPath() orelse VarPath{ .parts = &.{} };
        e.left = self.readExpr();
        e.right = self.readExpr();
        e.ternary_false = self.readExpr();

        const args_count = self.readU16() orelse 0xFFFF;
        if (args_count == 0xFFFF) {
            e.call_args = null;
        } else if (args_count > safety_limits.max_array_count) {
            e.call_args = null;
        } else if (args_count > 0) {
            const args = allocator.alloc(*Expr, args_count) catch {
                e.call_args = null;
                return e;
            };
            for (args, 0..) |*arg, i| {
                arg.* = self.readExpr() orelse {
                    e.call_args = args[0..i];
                    return e;
                };
            }
            e.call_args = args;
        } else {
            e.call_args = null;
        }
        return e;
    }
    fn readNode(self: *ByteReader) ?CompiledNode {
        const type_byte = self.readU8() orelse return null;
        // Validate NodeType enum
        const max_node_type = @intFromEnum(NodeType.trans_block);
        if (type_byte > max_node_type) return null;
        var node = CompiledNode{
            .type = @enumFromInt(type_byte),
            .ignore_missing = self.readBool() orelse false,
            .text = self.readString() orelse "",
            .block_name = self.readString() orelse "",
            .set_name = self.readString() orelse "",
            .for_var = self.readString() orelse "",
            .var_path = self.readVarPath() orelse VarPath{ .parts = &.{} },
            .for_iter = self.readVarPath() orelse VarPath{ .parts = &.{} },
            .filters = &.{},
            .for_iter_filters = &.{},
            .if_branches = &.{},
            .for_body = &.{},
            .for_empty = &.{},
            .children = &.{},
            .macro_params = &.{},
            .macro_args = &.{},
            .expr = null,
        };
        // Filters
        const filter_count = self.readU16() orelse 0;
        if (filter_count > 0) {
            if (allocator.alloc(FilterSpec, filter_count)) |filters| {
                for (filters, 0..) |*f, i| {
                    f.* = self.readFilterSpec() orelse {
                        node.filters = filters[0..i];
                        return node;
                    };
                }
                node.filters = filters;
            } else |_| {}
        }
        const fif_count = self.readU16() orelse 0;
        if (fif_count > 0) {
            if (allocator.alloc(FilterSpec, fif_count)) |fifs| {
                for (fifs, 0..) |*f, i| {
                    f.* = self.readFilterSpec() orelse {
                        node.for_iter_filters = fifs[0..i];
                        return node;
                    };
                }
                node.for_iter_filters = fifs;
            } else |_| {}
        }
        // If branches
        const branch_count = self.readU16() orelse 0;
        if (branch_count > 0) {
            const branches = allocator.alloc(IfBranch, branch_count) catch return node;
            for (branches, 0..) |*branch, i| {
                branch.condition_expr = self.readExpr();
                const body_count = self.readU32() orelse 0;
                branch.body = self.readNodeArray(body_count) orelse {
                    node.if_branches = branches[0..i];
                    return node;
                };
            }
            node.if_branches = branches;
        }
        // Child arrays
        const fb_count = self.readU32() orelse 0;
        node.for_body = self.readNodeArray(fb_count) orelse &.{};
        const fe_count = self.readU32() orelse 0;
        node.for_empty = self.readNodeArray(fe_count) orelse &.{};
        const ch_count = self.readU32() orelse 0;
        node.children = self.readNodeArray(ch_count) orelse &.{};
        // Super children
        const has_super = self.readU8() orelse 0;
        if (has_super != 0) {
            const sc_count = self.readU32() orelse 0;
            node.super_children = self.readNodeArray(sc_count);
        }
        // Macro params
        const mp_count = self.readU16() orelse 0;
        if (mp_count > 0) {
            const params = allocator.alloc(MacroParam, mp_count) catch return node;
            for (params) |*p| {
                p.name = self.readString() orelse "";
                const opt = self.readOptString();
                p.default_val = opt.val;
            }
            node.macro_params = params;
        }
        // Macro args
        const ma_count = self.readU16() orelse 0;
        if (ma_count > 0) {
            const margs = allocator.alloc([]const u8, ma_count) catch return node;
            for (margs) |*a| a.* = self.readString() orelse "";
            node.macro_args = margs;
        }
        // Expression
        node.expr = self.readExpr();
        return node;
    }
    fn readNodeArray(self: *ByteReader, count: u32) ?[]CompiledNode {
        if (count == 0) return &.{};
        if (count > safety_limits.max_array_count) {
            self.limit_exceeded = true;
            return null;
        }
        const nodes = allocator.alloc(CompiledNode, count) catch return null;
        for (nodes, 0..) |*n, i| {
            n.* = self.readNode() orelse {
                return nodes[0..i];
            };
        }
        return nodes;
    }
};

/// Serialize a compiled template to bytes. Returns owned byte slice.
pub fn serializeTemplate(tmpl: *const CompiledTemplate, source: []const u8) ?[]u8 {
    var w = ByteWriter{};

    // Header
    w.buf.appendSlice(allocator, &CACHE_MAGIC) catch return null;
    w.writeU16(CACHE_FORMAT_VERSION);
    w.writeU16(0); // reserved
    // Source hash (FNV-1a 64-bit)
    const hash = std.hash.Fnv1a_64.hash(source);
    w.buf.appendSlice(allocator, &std.mem.toBytes(hash)) catch return null;
    // Placeholder for total size (filled at end)
    const size_offset = w.buf.items.len;
    w.writeU32(0);
    // Node count
    w.writeU32(@intCast(tmpl.nodes.len));
    // Source path
    w.writeString(tmpl.source_path);
    // Root nodes
    for (tmpl.nodes) |*node| w.writeNode(node);
    // Dynamic extends
    w.writeExpr(tmpl.dynamic_extends_expr);
    if (tmpl.dynamic_extends_child_nodes) |cn| {
        w.writeU8(1);
        w.writeU32(@intCast(cn.len));
        for (cn) |*node| w.writeNode(node);
    } else {
        w.writeU8(0);
    }

    // Fill in total size
    const total: u32 = @intCast(w.buf.items.len);
    @memcpy(w.buf.items[size_offset..][0..4], &std.mem.toBytes(total));

    return w.buf.toOwnedSlice(allocator) catch null;
}

/// Deserialize a compiled template from bytes. Returns null on version/hash mismatch.
pub fn deserializeTemplate(data: []const u8, expected_hash: u64) ?*CompiledTemplate {
    if (data.len < 20) return null; // minimum header size

    var r = ByteReader{ .data = data };

    // Validate magic
    const m0 = r.readU8() orelse return null;
    const m1 = r.readU8() orelse return null;
    const m2 = r.readU8() orelse return null;
    const m3 = r.readU8() orelse return null;
    if (m0 != 'H' or m1 != 'Z' or m2 != 'T' or m3 != 'C') return null;

    // Validate version
    const version = r.readU16() orelse return null;
    if (version != CACHE_FORMAT_VERSION) return null;

    _ = r.readU16(); // reserved

    // Validate source hash
    const stored_hash = r.readI64() orelse return null;
    if (@as(u64, @bitCast(stored_hash)) != expected_hash) return null;

    _ = r.readU32(); // total size (could validate)
    const node_count = r.readU32() orelse return null;
    const source_path = r.readString() orelse return null;

    // Read root nodes
    const nodes = r.readNodeArray(node_count) orelse return null;

    // Dynamic extends
    const dyn_expr = r.readExpr();
    var dyn_child_nodes: ?[]CompiledNode = null;
    const has_dcn = r.readU8() orelse 0;
    if (has_dcn != 0) {
        const dcn_count = r.readU32() orelse 0;
        dyn_child_nodes = r.readNodeArray(dcn_count);
    }

    // If any safety limit was exceeded during reading, reject the entire template
    if (r.limit_exceeded) return null;

    // Build template
    const tmpl = allocator.create(CompiledTemplate) catch return null;
    tmpl.* = .{
        .nodes = nodes,
        .blocks = .{},
        .macros = .{},
        .source_path = source_path,
        .py_filters = .{},
        .dynamic_extends_expr = dyn_expr,
        .dynamic_extends_child_nodes = dyn_child_nodes,
    };

    // Rebuild block + macro indices from node tree
    indexBlocksRecursive(tmpl.nodes, &tmpl.blocks) catch {};
    for (nodes, 0..) |node, i| {
        if (node.type == .macro_def and node.block_name.len > 0) {
            tmpl.macros.put(allocator, node.block_name, i) catch {};
        }
    }

    return tmpl;
}

/// C API: _template_serialize(capsule) → bytes
pub fn py_template_serialize(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var capsule: ?*c.PyObject = null;
    var source_ptr: [*c]const u8 = null;
    var source_len: c.Py_ssize_t = 0;
    if (c.PyArg_ParseTuple(args, "Os#", &capsule, &source_ptr, &source_len) == 0) return null;

    const ptr = c.PyCapsule_GetPointer(capsule.?, "hyperdjango.compiled_template") orelse {
        py.setError("Invalid template capsule", .{});
        return null;
    };
    const tmpl: *const CompiledTemplate = @ptrCast(@alignCast(ptr));
    const source = source_ptr[0..@intCast(source_len)];

    const bytes = serializeTemplate(tmpl, source) orelse {
        py.setError("Failed to serialize template", .{});
        return null;
    };
    const result = c.PyBytes_FromStringAndSize(@ptrCast(bytes.ptr), @intCast(bytes.len));
    allocator.free(bytes);
    return result;
}

/// C API: _template_deserialize(bytes, source_hash_int) → capsule or None
pub fn py_template_deserialize(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var bytes_ptr: [*c]const u8 = null;
    var bytes_len: c.Py_ssize_t = 0;
    var hash_obj: ?*c.PyObject = null;
    if (c.PyArg_ParseTuple(args, "y#O", &bytes_ptr, &bytes_len, &hash_obj) == 0) return null;

    const data = bytes_ptr[0..@intCast(bytes_len)];
    // Extract u64 hash from Python int (may be > ssize_t max)
    const expected_hash: u64 = @bitCast(c.PyLong_AsUnsignedLongLong(hash_obj.?));
    if (expected_hash == @as(u64, @bitCast(@as(i64, -1))) and c.PyErr_Occurred() != null) {
        c.PyErr_Clear();
        return py.pyNone();
    }

    const tmpl = deserializeTemplate(data, expected_hash) orelse return py.pyNone();

    return c.PyCapsule_New(tmpl, "hyperdjango.compiled_template", templateCapsuleDestructor);
}
