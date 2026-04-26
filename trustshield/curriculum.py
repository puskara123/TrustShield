"""
Curriculum Controller
Manages difficulty tiers based on agent performance.
Person A owns this file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import random


_WINDOW_SIZE = 20
_PROMOTE_THRESHOLD = 0.70
_DEMOTE_THRESHOLD = 0.30


@dataclass
class CurriculumState:
	active_tier: int = 1
	unlocked_tiers: set[int] = field(default_factory=lambda: {1})
	recent_outcomes: dict[int, list[bool]] = field(default_factory=lambda: {1: [], 2: [], 3: []})
	rolling_win_rate: dict[int, float] = field(default_factory=lambda: {1: 0.0, 2: 0.0, 3: 0.0})


class CurriculumController:
	"""
	Auto-curriculum logic for TrustShield.
	Escalates tier when win rate > 70%, demotes when < 30%.
	"""

	def __init__(self):
		self.state_data = CurriculumState()

	def sample_tier(self) -> int:
		"""Randomly samples from unlocked tiers, weighted toward the highest unlocked."""
		highest = max(self.state_data.unlocked_tiers)
		if len(self.state_data.unlocked_tiers) == 1:
			return highest

		# 80% highest unlocked, 20% others
		if random.random() < 0.8:
			return highest
		return random.choice(list(self.state_data.unlocked_tiers - {highest}))

	def record_episode(self, tier: int, won: bool, reward: float):
		"""Update history and check for tier promotion/demotion."""
		outcomes = self.state_data.recent_outcomes[tier]
		outcomes.append(won)
		if len(outcomes) > _WINDOW_SIZE:
			outcomes.pop(0)

		# Update win rate
		if outcomes:
			self.state_data.rolling_win_rate[tier] = sum(outcomes) / len(outcomes)

		# Check for promotion (requires at least 10 samples to be stable)
		current_rate = self.state_data.rolling_win_rate[tier]
		if len(outcomes) >= _WINDOW_SIZE // 2:
			if current_rate >= _PROMOTE_THRESHOLD:
				if tier < 3:
					self.state_data.unlocked_tiers.add(tier + 1)
			elif current_rate <= _DEMOTE_THRESHOLD:
				# Demotion logic: could potentially lock tiers, but for hackathon 
				# we just stay at the current tier and hope for recovery.
				pass

	def state(self) -> dict:
		return {
			"active_tier": max(self.state_data.unlocked_tiers),
			"unlocked_tiers": sorted(list(self.state_data.unlocked_tiers)),
			"win_rates": self.state_data.rolling_win_rate,
		}
