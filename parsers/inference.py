"""Load the existing trained bundle without training or changing its algorithm."""
import hashlib
import json
from market_system.labor_market import LaborMarketPipeline
from market_system.paths import PROJECT_ROOT


class Inference:
    def __init__(self):
        models = PROJECT_ROOT/'parsers/models'
        model = models/'ensemble_20260924T064346Z_32b7a759'
        skills = models/'dictionaries/skill_lexicon.json'
        occupations = models/'dictionaries/occupation_lexicon.json'
        self.metadata = json.loads((model/'metadata.json').read_text(encoding='utf-8'))
        self.manifest = {'model':self.metadata,
                         'skill_lexicon_sha256':hashlib.sha256(skills.read_bytes()).hexdigest(),
                         'occupation_lexicon_sha256':hashlib.sha256(occupations.read_bytes()).hexdigest()}
        self.pipeline = LaborMarketPipeline.load(model,skill_lexicon=skills,occupation_lexicon=occupations)
        self.version = self.metadata['model_version']

    def analyze(self, text):
        return self.pipeline.analyze(text)
