import math

DECISION_POLICY = "strict_target_threshold_and_low_other_else_null"

def decide_label(prob_vac, prob_res, vacancy_threshold=0.8, resume_threshold=0.8,
                 max_other_probability=0.3):
    """Require a high target score and a strictly low competing score."""
    values = (prob_vac, prob_res, vacancy_threshold, resume_threshold, max_other_probability)
    if not all(math.isfinite(x) and 0 <= x <= 1 for x in values):
        raise ValueError("Probabilities and thresholds must be finite and in [0, 1]")
    if max_other_probability > min(vacancy_threshold, resume_threshold):
        raise ValueError("Competing-score ceiling must not exceed either target threshold")
    if prob_vac > vacancy_threshold and prob_res < max_other_probability:
        return "vacancy"
    if prob_res > resume_threshold and prob_vac < max_other_probability:
        return "resume"
    return None
