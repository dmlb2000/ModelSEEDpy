# -*- coding: utf-8 -*-
import logging
import itertools  # !!! the import is never used

logger = logging.getLogger(__name__)

import cobra
import re
from optlang.symbolics import Zero, add
from modelseedpy.core import FBAHelper  # !!! the import is never used
from modelseedpy.fbapkg.mspackagemanager import MSPackageManager
from modelseedpy.core.msmodelutl import MSModelUtil
from modelseedpy.core.exceptions import GapfillingError


class MSGapfill:
    @staticmethod
    def gapfill_count(solution):
        total = 0
        if "new" in solution:
            total += len(solution["new"])
        if "reversed" in solution:
            total += len(solution["reversed"])
        return total

    def __init__(
        self,
        model_or_mdlutl,
        default_gapfill_templates=[],
        default_gapfill_models=[],
        test_conditions=[],
        reaction_scores={},
        blacklist=[],
        atp_gapfilling=False,
    ):
        # Discerning input is model or mdlutl and setting internal links
        if isinstance(model_or_mdlutl, MSModelUtil):
            self.model = model_or_mdlutl.model
            self.mdlutl = model_or_mdlutl
        else:
            self.model = model_or_mdlutl
            self.mdlutl = MSModelUtil.get(model_or_mdlutl)
        # Setting gapfilling attribute in model utl so link is bidirectional
        if not atp_gapfilling:
            self.mdlutl.gfutl = self
        self.auto_sink = [
            "cpd02701",
            "cpd11416",
            "cpd15302",
            "cpd03091",
        ]  # the cpd11416 compound is filtered during model extension with templates
        self.gfmodel = self.lp_filename = self.last_solution = None
        self.model_penalty = 1
        self.default_gapfill_models = default_gapfill_models
        self.default_gapfill_templates = default_gapfill_templates
        self.gapfill_templates_by_index, self.gapfill_models_by_index = {}, {}
        self.gapfill_all_indecies_with_default_templates = True
        self.gapfill_all_indecies_with_default_models = True
        self.blacklist = list(set(blacklist))
        self.test_condition_iteration_limit = 10
        self.test_conditions = test_conditions
        self.reaction_scores = reaction_scores
        self.cumulative_gapfilling = []

    def run_gapfilling(
        self,
        media=None,
        target=None,
        minimum_obj=0.01,
        binary_check=False,
        prefilter=True,
        check_for_growth=True,
    ):
        if target:
            self.model.objective = self.model.problem.Objective(
                self.model.reactions.get_by_id(target).flux_expression, direction="max"
            )
        self.gfmodel = cobra.io.json.from_json(cobra.io.json.to_json(self.model))
        pkgmgr = MSPackageManager.get_pkg_mgr(self.gfmodel)
        pkgmgr.getpkg("GapfillingPkg").build_package(
            {
                "auto_sink": self.auto_sink,
                "model_penalty": self.model_penalty,
                "default_gapfill_models": self.default_gapfill_models,
                "default_gapfill_templates": self.default_gapfill_templates,
                "gapfill_templates_by_index": self.gapfill_templates_by_index,
                "gapfill_models_by_index": self.gapfill_models_by_index,
                "gapfill_all_indecies_with_default_templates": self.gapfill_all_indecies_with_default_templates,
                "gapfill_all_indecies_with_default_models": self.gapfill_all_indecies_with_default_models,
                "default_excretion": 100,
                "default_uptake": 100,
                "minimum_obj": minimum_obj,
                "blacklist": self.blacklist,
                "reaction_scores": self.reaction_scores,
                "set_objective": 1,
            }
        )
        pkgmgr.getpkg("KBaseMediaPkg").build_package(media)

        # Testing if gapfilling can work before filtering
        if (
            check_for_growth
            and not pkgmgr.getpkg("GapfillingPkg").test_gapfill_database()
        ):
            # save_json_model(self.model, "gfdebugmdl.json")
            gf_sensitivity = self.mdlutl.get_attributes("gf_sensitivity", {})
            if media.id not in gf_sensitivity:
                gf_sensitivity[media.id] = {}
            if target not in gf_sensitivity[media.id]:
                gf_sensitivity[media.id][target] = {}
            gf_sensitivity[media.id][target][
                "FBF"
            ] = self.mdlutl.find_unproducible_biomass_compounds(target)
            self.mdlutl.save_attributes(gf_sensitivity, "gf_sensitivity")
            logger.warning("No solution found before filtering for %s", media)
            return None

        # Filtering breaking reactions out of the database
        if prefilter and self.test_conditions:
            pkgmgr.getpkg("GapfillingPkg").filter_database_based_on_tests(
                self.test_conditions
            )

        # Testing if gapfilling can work after filtering
        if (
            check_for_growth
            and not pkgmgr.getpkg("GapfillingPkg").test_gapfill_database()
        ):
            # save_json_model(self.model, "gfdebugmdl.json")
            gf_sensitivity = self.mdlutl.get_attributes("gf_sensitivity", {})
            if media.id not in gf_sensitivity:
                gf_sensitivity[media.id] = {}
            if target not in gf_sensitivity[media.id]:
                gf_sensitivity[media.id][target] = {}
            gf_sensitivity[media.id][target][
                "FAF"
            ] = self.mdlutl.find_unproducible_biomass_compounds(target)
            self.mdlutl.save_attributes(gf_sensitivity, "gf_sensitivity")
            logger.warning("No solution found after filtering for %s", media)
            return None

        # Printing the gapfilling LP file
        if self.lp_filename:
            with open(self.lp_filename, "w") as out:
                out.write(str(self.gfmodel.solver))

        # Running gapfilling and checking solution
        sol = self.gfmodel.optimize()
        logger.debug(
            "gapfill solution objective value %f (%s) for media %s",
            sol.objective_value,
            sol.status,
            media,
        )
        if sol.status != "optimal":
            logger.warning("No solution found for %s", media)
            return None

        # Computing solution and ensuring all tests still pass
        self.last_solution = pkgmgr.getpkg("GapfillingPkg").compute_gapfilled_solution()
        if self.test_conditions:
            self.last_solution = pkgmgr.getpkg("GapfillingPkg").run_test_conditions(
                self.test_conditions,
                self.last_solution,
                self.test_condition_iteration_limit,
            )
            if self.last_solution is None:
                logger.warning(
                    "no solution could be found that satisfied all specified test conditions in specified iterations!"
                )
                return None

        # Running binary check to reduce solution to minimal reaction soltuion
        if binary_check:
            self.last_solution = pkgmgr.getpkg(
                "GapfillingPkg"
            ).binary_check_gapfilling_solution()

        # Setting last solution data
        self.last_solution["media"] = media
        self.last_solution["target"] = target
        self.last_solution["minobjective"] = minimum_obj
        self.last_solution["binary_check"] = binary_check
        return self.last_solution

    def integrate_gapfill_solution(
        self, solution, cumulative_solution=[], link_gaps_to_objective=True
    ):
        """Integrating gapfilling solution into model
        Parameters
        ----------
        solution : dict
            Specifies the reactions to be added to the model to implement the gapfilling solution
        cumulative_solution : list
            Optional array to cumulatively track all reactions added to the model when integrating multiple solutions
        """
        for rxn_id in solution["reversed"]:
            rxn = self.model.reactions.get_by_id(rxn_id)
            if solution["reversed"][rxn_id] == ">" and rxn.upper_bound <= 0:
                cumulative_solution.append([rxn_id, ">"])
                rxn.upper_bound = 100
            elif solution["reversed"][rxn_id] == "<" and rxn.lower_bound >= 0:
                cumulative_solution.append([rxn_id, "<"])
                rxn.lower_bound = -100
        for rxn_id in solution["new"]:
            if rxn_id not in self.model.reactions:
                rxn = self.gfmodel.reactions.get_by_id(rxn_id)
                rxn = rxn.copy()
                self.model.add_reactions([rxn])
                coreid = re.sub(r"_[a-z]\d+$", "", rxn_id)
                if coreid in self.reaction_scores:
                    bestgene = None
                    for gene in self.reaction_scores[coreid]:
                        if (
                            not bestgene
                            or self.reaction_scores[coreid][gene]
                            > self.reaction_scores[coreid][bestgene]
                        ):
                            bestgene = gene
                    rxn = self.model.reactions.get_by_id(rxn_id)
                    rxn.gene_reaction_rule = bestgene
                if solution["new"][rxn_id] == ">":
                    cumulative_solution.append([rxn_id, ">"])
                    rxn.upper_bound = 100
                    rxn.lower_bound = 0
                else:
                    cumulative_solution.append([rxn_id, "<"])
                    rxn.upper_bound = 0
                    rxn.lower_bound = -100
        unneeded = self.mdlutl.test_solution(
            solution, keep_changes=True
        )  # Strips out unneeded reactions - which undoes some of what is done above
        for item in unneeded:
            for oitem in cumulative_solution:
                if item[0] == oitem[0] and item[1] == oitem[1]:
                    cumulative_solution.remove(oitem)
                    break
        self.mdlutl.add_gapfilling(solution)
        if link_gaps_to_objective:
            gf_sensitivity = self.mdlutl.get_attributes("gf_sensitivity", {})
            if solution["media"] not in gf_sensitivity:
                gf_sensitivity[solution["media"]] = {}
            if solution["target"] not in gf_sensitivity[solution["media"]]:
                gf_sensitivity[solution["media"]][solution["target"]] = {}
            gf_sensitivity[solution["media"]][solution["target"]][
                "success"
            ] = self.mdlutl.find_unproducible_biomass_compounds(
                solution["target"], cumulative_solution
            )
            self.mdlutl.save_attributes(gf_sensitivity, "gf_sensitivity")
        self.cumulative_gapfilling.extend(cumulative_solution)

    @staticmethod
    def gapfill(
        model,
        media=None,
        target_reaction="bio1",
        default_gapfill_templates=[],
        default_gapfill_models=[],
        test_conditions=[],
        reaction_scores={},
        blacklist=[],
    ):
        gapfiller = MSGapfill(
            model,
            default_gapfill_templates,
            default_gapfill_models,
            test_conditions,
            reaction_scores,
            blacklist,
        )
        gfresults = gapfiller.run_gapfilling(media, target_reaction)
        return gapfiller.integrate_gapfill_solution(gfresults)
