/**
 * Harness Pi context extension (opt-in; only loaded when pi.harness_extension is true).
 *
 * Capability ceiling: this extension only (1) spills oversized tool output into the
 * Agent workspace, keeping a bounded preview in context, and (2) supplies a compaction
 * summary that preserves the Harness retention checklist. It adds no network
 * capability, cannot sign verdicts, never reads credentials or the run root, and every
 * file it writes is Agent-side output, not evidence.
 */
import { mkdir, writeFile } from "node:fs/promises";
import { convertToLlm, serializeConversation } from "@earendil-works/pi-coding-agent";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { uuidv7 } from "@earendil-works/pi-ai";

const MAX_INLINE_CHARS = 32000;
const PREVIEW_CHARS = 600;
const SPILL_DIR = "/run/harness/artifacts/tool-spill";

let spillSequence = 0;

const RETENTION_INSTRUCTIONS = `You are compacting a penetration-testing agent session. Produce one structured markdown summary that preserves, under these exact headings:

1. Scope and authorization constraints
2. Known assets, endpoints, parameters and technologies
3. Active hypotheses and their status
4. Confirmed findings with the evidence that confirms each one
5. Credential and authentication state (reference where they live; never echo secret values)
6. Evidence, artifacts and file paths worth returning to
7. Current phase and progress
8. Next actions

Discard verbose raw tool output, already-recorded dead ends, and duplicated scan noise. The summary replaces older conversation, so keep every fact the next step depends on.`;

type TextBlock = { type: "text"; text: string };

function isTextBlock(block: { type?: string; text?: string }): block is TextBlock {
	return block.type === "text" && typeof block.text === "string";
}

export default function harnessContext(pi: ExtensionAPI) {
	pi.on("tool_result", async (event) => {
		const textBlocks = event.content.filter(isTextBlock);
		const textChars = textBlocks
			.reduce((total, block) => total + block.text.length, 0);
		if (textChars <= MAX_INLINE_CHARS) return;

		// Spilling is a context optimisation, never a correctness requirement: if
		// the workspace cannot take the file, hand Pi the untouched tool result
		// back instead of failing the hook and dropping it.
		let spillPath = "";
		try {
			await mkdir(SPILL_DIR, { recursive: true });
			const callId = String(event.toolCallId ?? "");
			spillSequence += 1;
			const safeId =
				callId.replace(/[^A-Za-z0-9._-]/g, "-") ||
				`tool-${spillSequence.toString().padStart(4, "0")}`;
			spillPath = `${SPILL_DIR}/${safeId}.txt`;
			await writeFile(
				spillPath,
				textBlocks.map((block) => block.text).join("\n\n"),
				"utf8",
			);
		} catch {
			return;
		}

		let keptPreview = false;
		const content = event.content.map((block) => {
			if (!isTextBlock(block)) return block;
			if (!keptPreview) {
				keptPreview = true;
				return {
					...block,
					text: `${block.text.slice(0, PREVIEW_CHARS)}\n[full output: ${spillPath}]`,
				};
			}
			return { ...block, text: `[omitted: full output at ${spillPath}]` };
		});
		return { content, structuredContent: undefined };
	});

	pi.on("session_before_compact", async (event, ctx) => {
		if (ctx.signal?.aborted || !ctx.model) return;
		const {
			messagesToSummarize,
			turnPrefixMessages,
			tokensBefore,
			firstKeptEntryId,
			previousSummary,
		} = event.preparation;
		const messages = [...(messagesToSummarize ?? []), ...(turnPrefixMessages ?? [])];
		if (messages.length === 0) return;
		const previousContext = previousSummary
			? `\n\nPrevious summary for continuity:\n${previousSummary}`
			: "";
		const conversationText = serializeConversation(convertToLlm(messages));
		try {
			const response = await ctx.modelRegistry.complete(
				ctx.model,
				{
					messages: [
						{
							role: "user" as const,
							content: [
								{
									type: "text" as const,
									text: `${RETENTION_INSTRUCTIONS}${previousContext}\n\n<conversation>\n${conversationText}\n</conversation>`,
								},
							],
							timestamp: Date.now(),
						},
					],
				},
				{
					maxTokens: 8192,
					signal: event.signal,
					cacheRetention: "none",
					sessionId: uuidv7(),
				},
			);
			const summary = response.content
				.filter(
					(block): block is { type: "text"; text: string } =>
						block.type === "text",
				)
				.map((block) => block.text)
				.join("\n");
			if (!summary.trim() || event.signal.aborted) return;
			return {
				compaction: {
					summary,
					firstKeptEntryId,
					tokensBefore,
					usage: response.usage,
				},
			};
		} catch {
			// Any failure falls back to Pi's default compaction.
			return;
		}
	});
}
