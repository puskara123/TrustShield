"""
TrustShield: Social Engineering Defense Arena
"""

__version__ = "0.1.0"

def __getattr__(name):
	"""
	Lazy loading to prevent circular imports and reduce startup time.
	"""
	if name == "TrustShieldEnv":
		from .env import TrustShieldEnv
		return TrustShieldEnv
	if name == "Verifier":
		from .verifier import Verifier
		return Verifier
	if name == "CurriculumController":
		from .curriculum import CurriculumController
		return CurriculumController
	raise AttributeError(f"module {__name__} has no attribute {name}")
